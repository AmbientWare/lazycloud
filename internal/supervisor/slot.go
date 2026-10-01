package supervisor

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/runnerproto"
)

// killGrace is how long a runner's process group has between SIGTERM and
// SIGKILL.
const killGrace = 2 * time.Second

// runnerFD is the descriptor number of the runner's socket in the child.
const runnerFD = 3

// slot runs one runner process at a time and restarts it after a crash or a
// cancelled attempt.
type slot struct {
	sup *Supervisor

	// outMu orders output against attempt changes; see outputPipe.
	outMu     sync.Mutex
	buf       []byte
	proc      *runnerProcess
	attempt   string
	cancelled bool
}

// runnerProcess is one runner process and the resources it owns.
type runnerProcess struct {
	cmd    *exec.Cmd
	conn   net.Conn
	reader *bufio.Reader
	pipes  []*outputPipe
	// done is closed once the process has been reaped.
	done    chan struct{}
	waitErr error

	mu     sync.Mutex
	exited bool
	kill   *time.Timer

	stopReaders context.CancelFunc
	readers     sync.WaitGroup
}

func (sl *slot) run(ctx context.Context, cfg *hostproto.Configure, runs <-chan *hostproto.RunAttempt) error {
	for {
		p, err := sl.start(ctx, cfg)
		if err != nil {
			sl.sup.loadFailed(&hostproto.RunnerError{Type: "RunnerStartError", Message: err.Error()})
			return ErrLoadFailed
		}
		restart, err := sl.serve(ctx, p, cfg.GetHandler(), runs)
		sl.stop(p)
		if !restart || sl.sup.isDraining() {
			return err
		}
		sl.sup.log.Info("restarting runner", "exit", describeExit(p.waitErr))
	}
}

// serve loads the handler and runs attempts until the runner must restart
// (restart), the slot drained (nil) or the supervisor stops.
func (sl *slot) serve(ctx context.Context, p *runnerProcess, handler string, runs <-chan *hostproto.RunAttempt) (bool, error) {
	stop := context.AfterFunc(ctx, p.terminate)
	defer stop()
	if loadErr := p.load(handler); loadErr != nil {
		if ctx.Err() != nil {
			return false, fmt.Errorf("load handler: %w", ctx.Err())
		}
		sl.sup.loadFailed(loadErr)
		return false, ErrLoadFailed
	}
	sl.sup.slotLoaded(sl)
	loaded := time.Now()
	for {
		select {
		case <-ctx.Done():
			return false, fmt.Errorf("serve attempts: %w", ctx.Err())
		case <-p.done:
			// A runner that dies on its own right after loading restarts at
			// most once a second.
			if !sleepCtx(ctx, time.Second-time.Since(loaded)) {
				return false, fmt.Errorf("restart runner: %w", ctx.Err())
			}
			return true, nil
		case run, ok := <-runs:
			if !ok {
				return false, nil
			}
			if !sl.sup.begin(sl, run.GetAttemptId()) {
				continue
			}
			if !sl.invoke(p, run) {
				return true, nil
			}
		}
	}
}

// invoke runs one attempt and reports whether the runner can take another.
func (sl *slot) invoke(p *runnerProcess, run *hostproto.RunAttempt) bool {
	encoding, err := runnerEncoding(run.GetInputEncoding())
	if err != nil {
		return sl.finish(crashed(run.GetAttemptId(), "InvalidInput", err.Error()))
	}
	err = p.sendDependencies(run.GetDependencies())
	if err == nil {
		root := run.GetRootTaskId()
		if root == "" {
			root = run.GetTaskId()
		}
		err = p.send(runnerproto.Invoke{
			Type:          runnerproto.InvokeTypeInvoke,
			TaskId:        run.GetTaskId(),
			RootTaskId:    root,
			AttemptId:     run.GetAttemptId(),
			InputEncoding: encoding,
		}, run.GetInput())
	}
	var frame runnerproto.Frame
	if err == nil {
		frame, err = p.read()
	}
	if err == nil {
		var finished *hostproto.AttemptFinished
		if finished, err = attemptOutcome(run.GetAttemptId(), frame); err == nil {
			return sl.finish(finished)
		}
	}
	p.terminate()
	<-p.done
	message := fmt.Sprintf("runner exited during the attempt (%s)", describeExit(p.waitErr))
	if !errors.Is(err, errRunnerClosed) {
		message = fmt.Sprintf("runner protocol error: %v; %s", err, message)
	}
	sl.finish(crashed(run.GetAttemptId(), "RunnerCrashed", message))
	return false
}

// finish reports an attempt's outcome after its remaining output. A cancelled
// attempt reports nothing, since the server already recorded its outcome, and
// its runner is being killed.
func (sl *slot) finish(finished *hostproto.AttemptFinished) bool {
	sl.outMu.Lock()
	defer sl.outMu.Unlock()
	sl.drainLocked(true)
	cancelled := sl.cancelled
	sl.attempt, sl.cancelled = "", false
	if !cancelled {
		sl.sup.out.push(finishedMessage(finished))
	}
	return !cancelled
}

// start launches a runner in its own process group with a socket as fd 3.
func (sl *slot) start(ctx context.Context, cfg *hostproto.Configure) (_ *runnerProcess, err error) {
	command := cfg.GetRunnerCommand()
	if len(command) == 0 {
		return nil, errors.New("configure has no runner command")
	}
	var closers []func() error
	defer func() {
		if err != nil {
			for _, c := range closers {
				_ = c()
			}
		}
	}()
	fds, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		return nil, fmt.Errorf("create runner socket: %w", err)
	}
	parent := os.NewFile(uintptr(fds[0]), "runner-link") //nolint:gosec // descriptors are non-negative
	child := os.NewFile(uintptr(fds[1]), "runner-link")  //nolint:gosec // descriptors are non-negative
	defer func() { _ = child.Close() }()
	conn, err := net.FileConn(parent)
	_ = parent.Close()
	if err != nil {
		return nil, fmt.Errorf("open runner socket: %w", err)
	}
	closers = append(closers, conn.Close)

	p := &runnerProcess{conn: conn, reader: bufio.NewReaderSize(conn, 64<<10), done: make(chan struct{})}
	writers := make([]*os.File, 0, 2)
	for _, stream := range []hostproto.LogStream{hostproto.LogStream_LOG_STREAM_STDOUT, hostproto.LogStream_LOG_STREAM_STDERR} {
		r, w, err := os.Pipe()
		if err != nil {
			return nil, fmt.Errorf("create output pipe: %w", err)
		}
		closers = append(closers, r.Close, w.Close)
		pipe, err := newOutputPipe(r, stream)
		if err != nil {
			return nil, fmt.Errorf("open output pipe: %w", err)
		}
		p.pipes = append(p.pipes, pipe)
		writers = append(writers, w)
	}

	// The slot terminates the whole process group itself, so no context kills
	// only the leader.
	cmd := exec.Command(command[0], command[1:]...) //nolint:gosec,noctx // the agent chooses the runner command
	cmd.Dir = cfg.GetWorkingDirectory()
	cmd.Env = runnerEnvironment()
	cmd.Stdout, cmd.Stderr = writers[0], writers[1]
	cmd.ExtraFiles = []*os.File{child}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := sl.sup.children.start(cmd); err != nil {
		return nil, fmt.Errorf("start runner %q: %w", command[0], err)
	}
	for _, w := range writers {
		_ = w.Close()
	}
	p.cmd = cmd
	go p.wait(sl.sup.children)

	readCtx, stopReaders := context.WithCancel(ctx)
	p.stopReaders = stopReaders
	for _, pipe := range p.pipes {
		p.readers.Go(func() { sl.readOutput(readCtx, pipe) })
	}
	sl.outMu.Lock()
	sl.proc = p
	sl.outMu.Unlock()
	return p, nil
}

// stop closes the runner's socket, which asks it to exit, then escalates to
// signals. Remaining output is emitted before the pipes close.
func (sl *slot) stop(p *runnerProcess) {
	_ = p.conn.Close()
	select {
	case <-p.done:
	case <-time.After(killGrace):
		p.terminate()
		<-p.done
	}
	p.mu.Lock()
	if p.kill != nil {
		p.kill.Stop()
	}
	p.mu.Unlock()
	sl.outMu.Lock()
	sl.drainLocked(true)
	sl.proc = nil
	sl.outMu.Unlock()
	p.stopReaders()
	for _, pipe := range p.pipes {
		_ = pipe.file.Close()
	}
	p.readers.Wait()
}

// wait reaps the runner. Between exit and reaping the process group id
// cannot be reused, so its remaining members are killed then.
func (p *runnerProcess) wait(children *children) {
	pid := p.cmd.Process.Pid
	awaitExit(pid)
	p.mu.Lock()
	p.exited = true
	_ = unix.Kill(-pid, unix.SIGKILL)
	p.mu.Unlock()
	p.waitErr = p.cmd.Wait()
	children.remove(pid)
	// A descendant may still hold the socket; closing ours unblocks reads.
	_ = p.conn.Close()
	close(p.done)
}

func (p *runnerProcess) signal(sig unix.Signal) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.exited {
		_ = unix.Kill(-p.cmd.Process.Pid, sig)
	}
}

// terminate sends SIGTERM to the process group and SIGKILL after killGrace.
func (p *runnerProcess) terminate() {
	p.signal(unix.SIGTERM)
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.kill == nil && !p.exited {
		p.kill = time.AfterFunc(killGrace, func() { p.signal(unix.SIGKILL) })
	}
}

var errRunnerClosed = errors.New("runner closed its socket")

func (p *runnerProcess) send(header any, payload []byte) error {
	return closedAsRunnerClosed(runnerproto.WriteFrame(p.conn, header, payload))
}

// sendDependencies sends one dependency frame per upstream result ahead of
// the invoke that refers to them.
func (p *runnerProcess) sendDependencies(deps []*hostproto.DependencyResult) error {
	for _, dep := range deps {
		encoding, err := runnerEncoding(dep.GetEncoding())
		if err != nil {
			return fmt.Errorf("dependency %s: %w", dep.GetTaskId(), err)
		}
		header := runnerproto.Dependency{Type: runnerproto.DependencyTypeDependency, TaskId: dep.GetTaskId(), Encoding: encoding}
		if err := p.send(header, dep.GetData()); err != nil {
			return err
		}
	}
	return nil
}

func (p *runnerProcess) read() (runnerproto.Frame, error) {
	frame, err := runnerproto.ReadFrame(p.reader)
	return frame, closedAsRunnerClosed(err)
}

func closedAsRunnerClosed(err error) error {
	if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) || errors.Is(err, net.ErrClosed) ||
		errors.Is(err, unix.ECONNRESET) || errors.Is(err, unix.EPIPE) {
		return errRunnerClosed
	}
	return err
}

// load sends the handler and waits for the runner to import it.
func (p *runnerProcess) load(handler string) *hostproto.RunnerError {
	err := p.send(runnerproto.Load{Type: runnerproto.LoadTypeLoad, ProtocolVersion: runnerproto.N1, Handler: handler}, nil)
	var frame runnerproto.Frame
	if err == nil {
		frame, err = p.read()
	}
	if err == nil {
		switch frame.Type {
		case runnerproto.FrameLoaded:
			return nil
		case runnerproto.FrameLoadFailed:
			var failed runnerproto.LoadFailed
			if err = frame.Decode(&failed); err == nil {
				return runnerError(failed.Error)
			}
		case runnerproto.FrameLoad, runnerproto.FrameDependency, runnerproto.FrameInvoke, runnerproto.FrameSucceeded, runnerproto.FrameFailed:
			err = fmt.Errorf("unexpected %q frame while loading", frame.Type)
		default:
			err = fmt.Errorf("unknown %q frame while loading", frame.Type)
		}
	}
	p.terminate()
	<-p.done
	message := fmt.Sprintf("runner exited while loading the handler (%s)", describeExit(p.waitErr))
	if !errors.Is(err, errRunnerClosed) {
		message = fmt.Sprintf("runner protocol error: %v; %s", err, message)
	}
	return &hostproto.RunnerError{Type: "RunnerExited", Message: message}
}

func attemptOutcome(attempt string, frame runnerproto.Frame) (*hostproto.AttemptFinished, error) {
	switch frame.Type {
	case runnerproto.FrameSucceeded:
		var succeeded runnerproto.Succeeded
		if err := frame.Decode(&succeeded); err != nil {
			return nil, err
		}
		if succeeded.AttemptId != attempt {
			return nil, fmt.Errorf("result for attempt %q while running %q", succeeded.AttemptId, attempt)
		}
		encoding, err := hostEncoding(succeeded.ResultEncoding)
		if err != nil {
			return nil, err
		}
		return &hostproto.AttemptFinished{AttemptId: attempt, Outcome: &hostproto.AttemptFinished_Success{
			Success: &hostproto.TaskSuccess{Encoding: encoding, Result: frame.Payload},
		}}, nil
	case runnerproto.FrameFailed:
		var failed runnerproto.Failed
		if err := frame.Decode(&failed); err != nil {
			return nil, err
		}
		if failed.AttemptId != attempt {
			return nil, fmt.Errorf("failure for attempt %q while running %q", failed.AttemptId, attempt)
		}
		return &hostproto.AttemptFinished{AttemptId: attempt, Outcome: &hostproto.AttemptFinished_Failure{
			Failure: &hostproto.TaskFailure{
				Kind:      hostproto.AttemptFailureKind_ATTEMPT_FAILURE_KIND_USER_ERROR,
				Error:     runnerError(failed.Error),
				Exception: frame.Payload,
			},
		}}, nil
	case runnerproto.FrameLoad, runnerproto.FrameLoaded, runnerproto.FrameLoadFailed, runnerproto.FrameDependency, runnerproto.FrameInvoke:
		return nil, fmt.Errorf("unexpected %q frame during an attempt", frame.Type)
	}
	return nil, fmt.Errorf("unknown %q frame during an attempt", frame.Type)
}

func crashed(attempt, errorType, message string) *hostproto.AttemptFinished {
	return &hostproto.AttemptFinished{AttemptId: attempt, Outcome: &hostproto.AttemptFinished_Failure{
		Failure: &hostproto.TaskFailure{Kind: hostproto.AttemptFailureKind_ATTEMPT_FAILURE_KIND_CRASHED, Error: &hostproto.RunnerError{Type: errorType, Message: message}},
	}}
}

func runnerError(e runnerproto.RunnerError) *hostproto.RunnerError {
	out := &hostproto.RunnerError{Type: e.Type, Message: e.Message}
	if e.Traceback != nil {
		out.Traceback = *e.Traceback
	}
	return out
}

func runnerEncoding(e hostproto.PayloadEncoding) (runnerproto.Encoding, error) {
	switch e {
	case hostproto.PayloadEncoding_PAYLOAD_ENCODING_JSON:
		return runnerproto.Json, nil
	case hostproto.PayloadEncoding_PAYLOAD_ENCODING_CLOUDPICKLE:
		return runnerproto.Cloudpickle, nil
	case hostproto.PayloadEncoding_PAYLOAD_ENCODING_UNSPECIFIED:
	}
	return "", fmt.Errorf("unsupported input encoding %s", e)
}

func hostEncoding(e runnerproto.Encoding) (hostproto.PayloadEncoding, error) {
	switch e {
	case runnerproto.Json:
		return hostproto.PayloadEncoding_PAYLOAD_ENCODING_JSON, nil
	case runnerproto.Cloudpickle:
		return hostproto.PayloadEncoding_PAYLOAD_ENCODING_CLOUDPICKLE, nil
	}
	return hostproto.PayloadEncoding_PAYLOAD_ENCODING_UNSPECIFIED, fmt.Errorf("unsupported result encoding %q", e)
}

// runnerEnvironment is the supervisor's environment without its link socket,
// plus the runner's descriptor.
func runnerEnvironment() []string {
	env := make([]string, 0, len(os.Environ())+1)
	for _, kv := range os.Environ() {
		if !strings.HasPrefix(kv, SocketEnv+"=") {
			env = append(env, kv)
		}
	}
	return append(env, fmt.Sprintf("LAZYCLOUD_RUNNER_FD=%d", runnerFD))
}

func describeExit(err error) string {
	if err == nil {
		return "exit status 0"
	}
	return err.Error()
}

// sleepCtx waits for d and reports whether ctx is still live.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() == nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
