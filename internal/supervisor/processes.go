package supervisor

import (
	"crypto/rand"
	"errors"
	"fmt"
	"io/fs"
	"maps"
	"net/http"
	"os"
	"os/exec"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

const (
	// maxRetainedOutput is what a process keeps of each output stream.
	maxRetainedOutput = 256 << 10
	// maxOutputBudget bounds the output and metadata of every process
	// record together.
	maxOutputBudget = 64 << 20
	// maxProcessRecords bounds running and retained processes.
	maxProcessRecords = 65536
	// processRetention is how long an exited process's result stays.
	processRetention = 5 * time.Minute
	// outputDrainGrace is how long, counted from the exit, a result waits
	// for output a descendant is still writing.
	outputDrainGrace = time.Second
	maxWaitSeconds   = 5
)

// processTable owns the processes started through the control API and their
// results.
type processTable struct {
	children *children
	budget   atomic.Int64

	mu   sync.Mutex
	byID map[string]*process
	// finished holds exited processes in exit order, so expiry pops the
	// front.
	finished []*process
	// starting counts processes being started, toward maxProcessRecords.
	starting int
	closed   bool
	// work counts each process's waiter and output readers.
	work sync.WaitGroup
}

type process struct {
	id       string
	command  string
	cwd      string
	metadata int
	pipes    []*os.File

	mu         sync.Mutex
	pid        int
	running    bool
	alive      bool
	exitCode   int
	stdout     outputBuffer
	stderr     outputBuffer
	finishedAt time.Time
	// exited is closed when running becomes false.
	exited chan struct{}
}

type outputBuffer struct {
	data      []byte
	truncated bool
}

func newProcessTable(kids *children) *processTable {
	return &processTable{children: kids, byID: make(map[string]*process)}
}

func (t *processTable) reserve(n int) bool {
	for {
		used := t.budget.Load()
		if used+int64(n) > maxOutputBudget {
			return false
		}
		if t.budget.CompareAndSwap(used, used+int64(n)) {
			return true
		}
	}
}

// pruneLocked forgets results older than processRetention.
func (t *processTable) pruneLocked(now time.Time) {
	for len(t.finished) > 0 && now.Sub(t.finished[0].finishedAt) >= processRetention {
		p := t.finished[0]
		t.finished[0] = nil
		t.finished = t.finished[1:]
		delete(t.byID, p.id)
		p.mu.Lock()
		t.budget.Add(-int64(len(p.stdout.data) + len(p.stderr.data) + p.metadata))
		p.stdout.data, p.stderr.data = nil, nil
		p.mu.Unlock()
	}
}

var errRecordsFull = apiErr(http.StatusServiceUnavailable, apitypes.Unavailable, //nolint:gochecknoglobals // constant refusal
	"process result storage is full; retry after results expire")

// start runs args in their own process group with env and dir.
func (t *processTable) start(args []string, dir string, env []string) (*process, error) {
	p := &process{id: rand.Text(), command: strings.Join(args, " "), cwd: dir, exited: make(chan struct{})}
	p.metadata = len(p.id) + len(p.command) + len(p.cwd)
	t.mu.Lock()
	t.pruneLocked(time.Now())
	switch {
	case t.closed:
		t.mu.Unlock()
		return nil, apiErr(http.StatusServiceUnavailable, apitypes.Unavailable, "the container is stopping")
	case len(t.byID)+t.starting >= maxProcessRecords || !t.reserve(p.metadata):
		t.mu.Unlock()
		return nil, errRecordsFull
	}
	t.starting++
	t.mu.Unlock()
	err := t.launch(p, args, env)
	t.mu.Lock()
	defer t.mu.Unlock()
	t.starting--
	if err != nil {
		t.budget.Add(-int64(p.metadata))
		return nil, err
	}
	t.byID[p.id] = p
	if t.closed {
		_ = p.signal(unix.SIGKILL)
	}
	return p, nil
}

func (t *processTable) launch(p *process, args []string, env []string) (err error) {
	var writers []*os.File
	defer func() {
		for _, w := range writers {
			_ = w.Close()
		}
		if err != nil {
			for _, r := range p.pipes {
				_ = r.Close()
			}
		}
	}()
	for range 2 {
		r, w, err := os.Pipe()
		if err != nil {
			return fmt.Errorf("create output pipe: %w", err)
		}
		p.pipes = append(p.pipes, r)
		writers = append(writers, w)
	}
	cmd := exec.Command(args[0], args[1:]...) //nolint:gosec,noctx // the caller chooses the command; its group is signalled explicitly
	cmd.Dir, cmd.Env = p.cwd, env
	cmd.Stdout, cmd.Stderr = writers[0], writers[1]
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := t.children.start(cmd); err != nil {
		if errors.Is(err, exec.ErrNotFound) || errors.Is(err, fs.ErrNotExist) || errors.Is(err, fs.ErrPermission) ||
			errors.Is(err, syscall.ENOTDIR) || errors.Is(err, syscall.ENOEXEC) {
			return invalid("start %q: %v", args[0], err)
		}
		return err
	}
	p.pid, p.running, p.alive = cmd.Process.Pid, true, true
	var readers sync.WaitGroup
	for i, buf := range []*outputBuffer{&p.stdout, &p.stderr} {
		readers.Add(1)
		t.work.Go(func() {
			defer readers.Done()
			t.capture(p, p.pipes[i], buf)
		})
	}
	t.work.Go(func() { t.await(p, cmd, &readers) })
	return nil
}

// capture keeps the first maxRetainedOutput bytes of a stream within the
// budget and reads the rest away, so the writer never blocks.
func (t *processTable) capture(p *process, pipe *os.File, buf *outputBuffer) {
	defer func() { _ = pipe.Close() }()
	chunk := make([]byte, readChunk)
	for {
		n, err := pipe.Read(chunk)
		if n > 0 {
			p.mu.Lock()
			if p.running && !buf.truncated {
				data := chunk[:n]
				if room := maxRetainedOutput - len(buf.data); len(data) > room {
					data, buf.truncated = data[:room], true
				}
				if t.reserve(len(data)) {
					buf.data = append(buf.data, data...)
				} else {
					buf.truncated = true
				}
			}
			p.mu.Unlock()
		}
		if err != nil {
			return
		}
	}
}

// await records the exit once the output has ended, or outputDrainGrace
// after the exit when a descendant still holds the pipes; that output is
// marked truncated and read away.
func (t *processTable) await(p *process, cmd *exec.Cmd, readers *sync.WaitGroup) {
	err := t.children.wait(cmd, func() {
		p.mu.Lock()
		p.alive = false
		p.mu.Unlock()
	})
	drained := make(chan struct{})
	go func() {
		readers.Wait()
		close(drained)
	}()
	incomplete := false
	grace := time.NewTimer(outputDrainGrace)
	select {
	case <-drained:
	case <-grace.C:
		incomplete = true
	}
	grace.Stop()
	t.mu.Lock()
	defer t.mu.Unlock()
	p.mu.Lock()
	p.running, p.exitCode, p.finishedAt = false, exitCode(err), time.Now()
	p.stdout.truncated = p.stdout.truncated || incomplete
	p.stderr.truncated = p.stderr.truncated || incomplete
	close(p.exited)
	p.mu.Unlock()
	t.finished = append(t.finished, p)
}

func (t *processTable) get(id string) (*process, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.pruneLocked(time.Now())
	p := t.byID[id]
	if p == nil {
		return nil, apiErr(http.StatusNotFound, apitypes.NotFound, "process %s expired or does not exist", id)
	}
	return p, nil
}

func (t *processTable) list() []apitypes.ProcessSummary {
	t.mu.Lock()
	t.pruneLocked(time.Now())
	procs := make([]*process, 0, len(t.byID))
	for _, p := range t.byID {
		procs = append(procs, p)
	}
	t.mu.Unlock()
	out := make([]apitypes.ProcessSummary, 0, len(procs))
	for _, p := range procs {
		v := p.view()
		out = append(out, apitypes.ProcessSummary{
			ProcessId: v.ProcessId, Pid: v.Pid, Command: v.Command, Cwd: v.Cwd, Running: v.Running, ExitCode: v.ExitCode,
		})
	}
	slices.SortFunc(out, func(a, b apitypes.ProcessSummary) int { return a.Pid - b.Pid })
	return out
}

// signal sends sig to the process's group while its leader is alive; an
// exited process is not an error.
func (p *process) signal(sig unix.Signal) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.alive {
		return nil
	}
	if err := unix.Kill(-p.pid, sig); err != nil && !errors.Is(err, unix.ESRCH) {
		return fmt.Errorf("signal process group %d: %w", p.pid, err)
	}
	return nil
}

func (p *process) view() apitypes.Process {
	p.mu.Lock()
	defer p.mu.Unlock()
	cwd := p.cwd
	v := apitypes.Process{
		ProcessId: p.id, Pid: p.pid, Command: p.command, Cwd: &cwd, Running: p.running,
		Stdout: string(p.stdout.data), Stderr: string(p.stderr.data),
		StdoutTruncated: p.stdout.truncated, StderrTruncated: p.stderr.truncated,
	}
	if !p.running {
		code, expires := p.exitCode, p.finishedAt.Add(processRetention)
		v.ExitCode, v.ExpiresAt = &code, &expires
	}
	return v
}

// close kills every process group still running, unblocks output a
// descendant holds open and waits for the table's goroutines.
func (t *processTable) close() {
	t.mu.Lock()
	t.closed = true
	procs := make([]*process, 0, len(t.byID))
	for _, p := range t.byID {
		procs = append(procs, p)
	}
	t.mu.Unlock()
	for _, p := range procs {
		_ = p.signal(unix.SIGKILL)
	}
	for _, p := range procs {
		select {
		case <-p.exited:
		case <-time.After(outputDrainGrace):
		}
		for _, pipe := range p.pipes {
			_ = pipe.Close()
		}
	}
	t.work.Wait()
}

var killSignals = map[apitypes.KillRequestSignal]unix.Signal{ //nolint:gochecknoglobals // constant table
	"HUP": unix.SIGHUP, "INT": unix.SIGINT, "KILL": unix.SIGKILL, "QUIT": unix.SIGQUIT,
	"TERM": unix.SIGTERM, "USR1": unix.SIGUSR1, "USR2": unix.SIGUSR2,
}

func (c *control) startProcess(w http.ResponseWriter, r *http.Request) error {
	var req apitypes.ProcessRequest
	if err := readJSON(r, &req, false); err != nil {
		return err
	}
	if len(req.Args) == 0 || req.Args[0] == "" {
		return invalid("args must name a program")
	}
	dir := c.workspace
	if req.Cwd != nil {
		var err error
		if dir, err = c.resolve(*req.Cwd); err != nil {
			return err
		}
	}
	env := c.env
	if req.Env != nil {
		overrides := make([]string, 0, len(*req.Env))
		for _, name := range slices.Sorted(maps.Keys(*req.Env)) {
			if name == "" || strings.ContainsAny(name, "=\x00") {
				return invalid("invalid environment variable name %q", name)
			}
			overrides = append(overrides, name+"="+(*req.Env)[name])
		}
		env = mergeEnvironment(env, overrides...)
	}
	p, err := c.procs.start(req.Args, dir, env)
	if err != nil {
		return err
	}
	writeJSON(w, http.StatusCreated, p.view())
	return nil
}

func (c *control) listProcesses(w http.ResponseWriter, _ *http.Request) error {
	writeJSON(w, http.StatusOK, apitypes.ProcessList{Processes: c.procs.list()})
	return nil
}

func (c *control) getProcess(w http.ResponseWriter, r *http.Request) error {
	wait := 0.0
	if raw := r.URL.Query().Get("wait_seconds"); raw != "" {
		var err error
		if wait, err = strconv.ParseFloat(raw, 64); err != nil || wait < 0 || wait > maxWaitSeconds {
			return invalid("wait_seconds must be a number from 0 to %d", maxWaitSeconds)
		}
	}
	p, err := c.procs.get(r.PathValue("id"))
	if err != nil {
		return err
	}
	if wait > 0 {
		timer := time.NewTimer(time.Duration(wait * float64(time.Second)))
		select {
		case <-p.exited:
		case <-timer.C:
		case <-r.Context().Done():
		}
		timer.Stop()
	}
	writeJSON(w, http.StatusOK, p.view())
	return nil
}

func (c *control) killProcess(w http.ResponseWriter, r *http.Request) error {
	var req apitypes.KillRequest
	if err := readJSON(r, &req, true); err != nil {
		return err
	}
	sig := unix.SIGTERM
	if req.Signal != nil {
		known, ok := killSignals[*req.Signal]
		if !ok {
			return invalid("unknown signal %q", *req.Signal)
		}
		sig = known
	}
	p, err := c.procs.get(r.PathValue("id"))
	if err != nil {
		return err
	}
	if err := p.signal(sig); err != nil {
		return err
	}
	w.WriteHeader(http.StatusNoContent)
	return nil
}
