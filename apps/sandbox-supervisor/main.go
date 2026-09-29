package main

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"os/signal"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const (
	protocolVersion        = 1
	listenAddress          = "0.0.0.0:7111"
	defaultTokenPath       = "/run/lazycloud/sandbox-supervisor.token"
	maxPendingLogBytes     = 1024 * 1024
	maxRetainedOutputBytes = 256 * 1024
	maxProcessRecords      = 65536
	maxProcessBufferBytes  = 64 * 1024 * 1024
	processResultRetention = 5 * time.Minute
	// outputDrainGrace is the longest an exec'd process's exit report waits,
	// counted from the exit, for output a descendant is still writing.
	outputDrainGrace = time.Second
)

type request struct {
	Version     int      `json:"version"`
	Op          string   `json:"op"`
	Argv        []string `json:"argv,omitempty"`
	Cwd         string   `json:"cwd,omitempty"`
	Env         []string `json:"env,omitempty"`
	PID         int      `json:"pid,omitempty"`
	ProcessID   string   `json:"process_id,omitempty"`
	WaitSeconds float64  `json:"wait_seconds,omitempty"`
	Signal      int      `json:"signal,omitempty"`
	AckSeq      uint64   `json:"ack_seq,omitempty"`
	OK          bool     `json:"ok,omitempty"`
	Token       string   `json:"token,omitempty"`
	// Payload is the file helper's request for the filesystem operation.
	Payload string `json:"payload,omitempty"`
}

type response struct {
	Version         int           `json:"version"`
	Type            string        `json:"type"`
	Error           string        `json:"error,omitempty"`
	PID             int           `json:"pid,omitempty"`
	Seq             uint64        `json:"seq,omitempty"`
	Stream          string        `json:"stream,omitempty"`
	Data            []byte        `json:"data,omitempty"`
	ExitCode        int           `json:"exit_code,omitempty"`
	Running         bool          `json:"running,omitempty"`
	Processes       []processView `json:"processes,omitempty"`
	Stdout          string        `json:"stdout,omitempty"`
	Stderr          string        `json:"stderr,omitempty"`
	ProcessID       string        `json:"process_id,omitempty"`
	StdoutTruncated bool          `json:"stdout_truncated,omitempty"`
	StderrTruncated bool          `json:"stderr_truncated,omitempty"`
	ExpiresAt       *time.Time    `json:"expires_at,omitempty"`
}

type processView struct {
	ProcessID string `json:"process_id"`
	PID       int    `json:"pid"`
	Command   string `json:"command"`
	Cwd       string `json:"cwd"`
	Running   bool   `json:"running"`
	ExitCode  int    `json:"exit_code"`
}

type logChunk struct {
	Seq    uint64
	Stream string
	Data   []byte
}

type processState struct {
	mu              sync.Mutex
	pid             int
	command         string
	cwd             string
	running         bool
	exitCode        int
	nextSeq         uint64
	ackSeq          uint64
	logs            []logChunk
	changed         chan struct{}
	process         *os.Process
	alive           bool
	pendingBytes    int
	metadataBytes   int
	stdout          []byte
	stderr          []byte
	finishedAt      time.Time
	id              string
	budget          *atomic.Int64
	stdoutTruncated bool
	stderrTruncated bool
	readers         int
}

func (p *processState) notify() {
	close(p.changed)
	p.changed = make(chan struct{})
}

type supervisor struct {
	mu             sync.RWMutex
	processes      map[int]*processState
	directChildren map[int]struct{}
	connections    map[net.Conn]struct{}
	tokenPath      string
	workload       *exec.Cmd
	workloadExit   chan int
	stopping       bool
	results        map[string]*processState
	bufferBytes    atomic.Int64
}

func main() {
	if len(os.Args) == 3 && os.Args[1] == "filesystem" {
		if err := runFilesystem(os.Args[2]); err != nil {
			if errors.Is(err, errOverLimit) {
				_, _ = fmt.Fprintln(os.Stderr, err)
				os.Exit(exitOverLimit)
			}
			fatal(err)
		}
		return
	}
	if len(os.Args) == 3 && os.Args[1] == "snapshot-filesystem" {
		if err := snapshotFilesystem(os.Args[2]); err != nil {
			fatal(err)
		}
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "shell" {
		if err := runShell(os.Args[2:]); err != nil {
			fatal(err)
		}
		return
	}
	s := &supervisor{
		processes:      make(map[int]*processState),
		directChildren: make(map[int]struct{}),
		connections:    make(map[net.Conn]struct{}),
		tokenPath:      defaultTokenPath,
	}
	args := os.Args[1:]
	sshEnabled := len(args) > 0 && args[0] == "--ssh"
	if sshEnabled {
		args = args[1:]
	}
	if len(args) > 0 {
		if args[0] != "--" || len(args) < 2 {
			fatal(errors.New("expected -- followed by a workload command"))
		}
		s.workload = exec.Command(args[1], args[2:]...)
		s.workload.Stdin = os.Stdin
		s.workload.Stdout = os.Stdout
		s.workload.Stderr = os.Stderr
		s.workload.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		s.workloadExit = make(chan int, 1)
	}
	if _, err := s.controlToken(); err != nil {
		fatal(err)
	}
	go s.reapAdoptedChildren()
	go func() {
		for range time.Tick(time.Second) {
			s.pruneExitedProcesses()
		}
	}()
	if sshEnabled {
		sshListener, sshErr := startSSHServer(s)
		if sshErr != nil {
			fatal(sshErr)
		}
		defer sshListener.Close()
	}
	listener, err := net.Listen("tcp", listenAddress)
	if err != nil {
		fatal(err)
	}
	defer listener.Close()
	go s.handleSignals()
	if s.workload != nil {
		go func() {
			os.Exit(<-s.workloadExit)
		}()
	}
	for {
		connection, acceptErr := listener.Accept()
		if acceptErr != nil {
			if errors.Is(acceptErr, net.ErrClosed) {
				return
			}
			continue
		}
		s.trackConnection(connection)
		go s.handleConnection(connection)
	}
}

func (s *supervisor) handleSignals() {
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	received := <-signals
	signalNumber := received.(syscall.Signal)
	s.mu.Lock()
	s.stopping = true
	hasWorkload := s.workload != nil && s.workload.Process != nil
	if hasWorkload {
		_ = syscall.Kill(-s.workload.Process.Pid, signalNumber)
	}
	processes := make([]*processState, 0, len(s.processes))
	for _, process := range s.processes {
		processes = append(processes, process)
	}
	s.mu.Unlock()
	for _, process := range processes {
		process.mu.Lock()
		if process.alive && process.process != nil {
			_ = syscall.Kill(-process.pid, signalNumber)
		}
		process.mu.Unlock()
	}
	if hasWorkload {
		return
	}
	time.Sleep(250 * time.Millisecond)
	os.Exit(128 + int(signalNumber))
}

func (s *supervisor) handleConnection(connection net.Conn) {
	defer func() {
		s.untrackConnection(connection)
		_ = connection.Close()
	}()
	decoder := json.NewDecoder(bufio.NewReader(connection))
	encoder := json.NewEncoder(connection)
	var command request
	if err := decoder.Decode(&command); err != nil {
		return
	}
	if command.Version != protocolVersion {
		_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "unsupported protocol version"})
		return
	}
	if !s.authenticated(command.Token) {
		_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "unauthorized"})
		return
	}
	switch command.Op {
	case "ready":
		_ = encoder.Encode(response{Version: protocolVersion, Type: "ready"})
	case "start-workload":
		s.handleStartWorkload(encoder)
	case "drain":
		s.handleDrain(encoder, connection)
	case "exec":
		s.handleExec(decoder, encoder, command)
	case "filesystem":
		s.handleFilesystem(encoder, command.Payload)
	case "watch":
		s.handleWatch(decoder, encoder, command.ProcessID, command.AckSeq)
	case "status":
		s.handleStatus(encoder, command.PID)
	case "result":
		s.handleResult(encoder, command)
	case "stdout", "stderr":
		s.handleOutput(encoder, command.PID, command.Op)
	case "list":
		s.handleList(encoder)
	case "kill":
		s.handleKill(encoder, command.PID, command.ProcessID, command.Signal)
	default:
		_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "unknown operation"})
	}
}

func (s *supervisor) handleStartWorkload(encoder *json.Encoder) {
	pid, err := s.startWorkload()
	if err != nil {
		writeError(encoder, err)
		return
	}
	_ = encoder.Encode(response{Version: protocolVersion, Type: "started", PID: pid})
}

func (s *supervisor) startWorkload() (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stopping {
		return 0, errors.New("supervisor is stopping")
	}
	if s.workload == nil {
		return 0, errors.New("supervisor has no workload command")
	}
	if s.workload.Process != nil {
		return s.workload.Process.Pid, nil
	}
	if err := s.workload.Start(); err != nil {
		s.workloadExit <- 1
		return 0, err
	}
	pid := s.workload.Process.Pid
	s.directChildren[pid] = struct{}{}
	go func() {
		err := s.workload.Wait()
		s.mu.Lock()
		delete(s.directChildren, pid)
		s.mu.Unlock()
		exitCode := 0
		if err != nil {
			exitCode = 1
			var exitError *exec.ExitError
			if errors.As(err, &exitError) {
				exitCode = exitError.ExitCode()
				if status, ok := exitError.ProcessState.Sys().(syscall.WaitStatus); ok && status.Signaled() {
					exitCode = 128 + int(status.Signal())
				}
			}
		}
		s.workloadExit <- exitCode
	}()
	return pid, nil
}

func (s *supervisor) trackConnection(connection net.Conn) {
	s.mu.Lock()
	s.connections[connection] = struct{}{}
	s.mu.Unlock()
}

func (s *supervisor) untrackConnection(connection net.Conn) {
	s.mu.Lock()
	delete(s.connections, connection)
	s.mu.Unlock()
}

func (s *supervisor) controlToken() ([]byte, error) {
	token, err := os.ReadFile(s.tokenPath)
	if err != nil {
		return nil, fmt.Errorf("read supervisor credential: %w", err)
	}
	token = []byte(strings.TrimSpace(string(token)))
	if len(token) < 32 {
		return nil, errors.New("supervisor credential is invalid")
	}
	return token, nil
}

func (s *supervisor) authenticated(provided string) bool {
	expected, err := s.controlToken()
	if err != nil || len(expected) != len(provided) {
		return false
	}
	return subtle.ConstantTimeCompare(expected, []byte(provided)) == 1
}

func (s *supervisor) handleDrain(encoder *json.Encoder, current net.Conn) {
	s.mu.Lock()
	connections := make([]net.Conn, 0, len(s.connections))
	for connection := range s.connections {
		if connection != current {
			connections = append(connections, connection)
			delete(s.connections, connection)
		}
	}
	s.mu.Unlock()
	for _, connection := range connections {
		if err := connection.Close(); err != nil {
			writeError(encoder, fmt.Errorf("close accepted connection: %w", err))
			return
		}
	}
	_ = encoder.Encode(response{Version: protocolVersion, Type: "drained"})
}

func (s *supervisor) handleExec(decoder *json.Decoder, encoder *json.Encoder, command request) {
	if len(command.Argv) == 0 {
		_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "argv is required"})
		return
	}
	s.mu.Lock()
	if s.results == nil {
		s.results = make(map[string]*processState)
	}
	if len(s.results) >= maxProcessRecords {
		s.mu.Unlock()
		writeError(encoder, errors.New("sandbox process result storage is full; retry after results expire"))
		return
	}
	state := &processState{id: rand.Text(), budget: &s.bufferBytes, running: true, changed: make(chan struct{})}
	state.command = strings.Join(command.Argv, " ")
	state.cwd = command.Cwd
	state.metadataBytes = len(state.command) + len(state.cwd) + len(state.id)
	if !state.reserveBytes(state.metadataBytes) {
		s.mu.Unlock()
		writeError(encoder, errors.New("sandbox process result storage is full; retry after results expire"))
		return
	}
	s.results[state.id] = state
	s.mu.Unlock()
	started := false
	defer func() {
		if !started {
			s.mu.Lock()
			delete(s.results, state.id)
			state.releaseBytes(state.metadataBytes)
			s.mu.Unlock()
		}
	}()
	cmd := exec.Command(command.Argv[0], command.Argv[1:]...)
	cmd.Dir = command.Cwd
	cmd.Env = command.Env
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	// The supervisor owns these pipes. Wait closes a StdoutPipe as soon as the
	// process exits, which loses whatever it wrote last and kills descendants
	// still writing.
	stdout, stdoutWriter, err := os.Pipe()
	if err != nil {
		writeError(encoder, err)
		return
	}
	stderr, stderrWriter, err := os.Pipe()
	if err != nil {
		_ = stdout.Close()
		_ = stdoutWriter.Close()
		writeError(encoder, err)
		return
	}
	cmd.Stdout = stdoutWriter
	cmd.Stderr = stderrWriter
	err = s.startChild(cmd)
	_ = stdoutWriter.Close()
	_ = stderrWriter.Close()
	if err != nil {
		_ = stdout.Close()
		_ = stderr.Close()
		writeError(encoder, err)
		return
	}
	state.mu.Lock()
	state.pid = cmd.Process.Pid
	state.exitCode = -1
	state.nextSeq = 1
	state.process = cmd.Process
	state.alive = true
	state.mu.Unlock()
	started = true
	s.mu.Lock()
	s.processes[state.pid] = state
	s.mu.Unlock()
	var outputReaders sync.WaitGroup
	outputReaders.Add(2)
	go captureOutput(state, "stdout", stdout, &outputReaders)
	go captureOutput(state, "stderr", stderr, &outputReaders)
	go s.waitProcess(state, cmd, &outputReaders)
	if err := encoder.Encode(response{Version: protocolVersion, Type: "started", PID: state.pid, ProcessID: state.id, Running: true}); err != nil {
		return
	}
	s.streamProcess(decoder, encoder, state, 0)
}

func captureOutput(state *processState, stream string, reader io.ReadCloser, done *sync.WaitGroup) {
	defer done.Done()
	defer reader.Close()
	buffer := make([]byte, 32*1024)
	for {
		count, err := reader.Read(buffer)
		if count > 0 {
			state.mu.Lock()
			if !state.running {
				state.mu.Unlock()
				continue
			}
			data := append([]byte(nil), buffer[:count]...)
			if state.reserveBytes(len(data)) {
				state.logs = append(state.logs, logChunk{Seq: state.nextSeq, Stream: stream, Data: data})
				state.pendingBytes += len(data)
			}
			for state.pendingBytes > maxPendingLogBytes && len(state.logs) > 0 {
				state.pendingBytes -= len(state.logs[0].Data)
				state.releaseBytes(len(state.logs[0].Data))
				state.logs[0] = logChunk{}
				state.logs = state.logs[1:]
			}
			if stream == "stdout" {
				state.stdout, state.stdoutTruncated = state.retainOutput(state.stdout, data, state.stdoutTruncated)
			} else {
				state.stderr, state.stderrTruncated = state.retainOutput(state.stderr, data, state.stderrTruncated)
			}
			state.nextSeq++
			state.notify()
			state.mu.Unlock()
		}
		if err != nil {
			return
		}
	}
}

// waitProcess reports the exit once the output has ended, or once
// outputDrainGrace has passed since the exit if a descendant still holds the
// pipes. The pipes stay open and read after that, so the descendant keeps
// running.
func (s *supervisor) waitProcess(state *processState, cmd *exec.Cmd, outputReaders *sync.WaitGroup) {
	err := s.waitChild(cmd, func() {
		state.mu.Lock()
		state.alive = false
		state.mu.Unlock()
	})
	drained := make(chan struct{})
	go func() {
		outputReaders.Wait()
		close(drained)
	}()
	grace := time.NewTimer(outputDrainGrace)
	outputIncomplete := false
	select {
	case <-drained:
	case <-grace.C:
		outputIncomplete = true
	}
	grace.Stop()
	exitCode := exitCodeOf(err)
	state.mu.Lock()
	state.running = false
	state.exitCode = exitCode
	state.stdoutTruncated = state.stdoutTruncated || outputIncomplete
	state.stderrTruncated = state.stderrTruncated || outputIncomplete
	state.finishedAt = time.Now()
	state.notify()
	state.mu.Unlock()
}

func exitCodeOf(err error) int {
	if err == nil {
		return 0
	}
	var exitError *exec.ExitError
	if !errors.As(err, &exitError) {
		return 1
	}
	if status, ok := exitError.ProcessState.Sys().(syscall.WaitStatus); ok && status.Signaled() {
		return 128 + int(status.Signal())
	}
	return exitError.ExitCode()
}

// handleFilesystem runs the file helper and streams all of its stdout, then
// its exit status and stderr. Nothing is logged or replayed, so nothing is
// dropped, and the connection's flow control paces the helper. The helper
// starts no processes of its own, so its stdout ends when it does.
func (s *supervisor) handleFilesystem(encoder *json.Encoder, payload string) {
	executable, err := os.Executable()
	if err != nil {
		writeError(encoder, err)
		return
	}
	cmd := exec.Command(executable, "filesystem", payload)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		writeError(encoder, err)
		return
	}
	if err := s.startChild(cmd); err != nil {
		writeError(encoder, err)
		return
	}
	buffer := make([]byte, 32*1024)
	var sendErr error
	for {
		count, readErr := stdout.Read(buffer)
		if count > 0 && sendErr == nil {
			sendErr = encoder.Encode(response{Version: protocolVersion, Type: "chunk", Stream: "stdout", Data: buffer[:count]})
			if sendErr != nil {
				_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
			}
		}
		if readErr != nil {
			break
		}
	}
	err = s.waitChild(cmd, func() {})
	if sendErr != nil {
		return
	}
	_ = encoder.Encode(response{Version: protocolVersion, Type: "exited", ExitCode: exitCodeOf(err), Stderr: stderr.String()})
}

func (s *supervisor) pruneExitedProcesses() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for id, state := range s.results {
		state.mu.Lock()
		if !state.running && state.readers == 0 && time.Since(state.finishedAt) >= processResultRetention {
			delete(s.results, id)
			if s.processes[state.pid] == state {
				delete(s.processes, state.pid)
			}
			state.releaseBytes(len(state.stdout) + len(state.stderr) + state.pendingBytes + state.metadataBytes)
			state.stdout, state.stderr, state.logs = nil, nil, nil
			state.pendingBytes = 0
		}
		state.mu.Unlock()
	}
}

func (p *processState) reserveBytes(size int) bool {
	for {
		used := p.budget.Load()
		if used+int64(size) > maxProcessBufferBytes {
			return false
		}
		if p.budget.CompareAndSwap(used, used+int64(size)) {
			return true
		}
	}
}

func (p *processState) releaseBytes(size int) {
	p.budget.Add(-int64(size))
}

func (p *processState) retainOutput(existing, data []byte, truncated bool) ([]byte, bool) {
	if truncated {
		return existing, true
	}
	remaining := maxRetainedOutputBytes - len(existing)
	if len(data) > remaining {
		data, truncated = data[:remaining], true
	}
	if !p.reserveBytes(len(data)) {
		return existing, true
	}
	return append(existing, data...), truncated
}

func (s *supervisor) handleResult(encoder *json.Encoder, command request) {
	if command.WaitSeconds < 0 || command.WaitSeconds > 5 {
		writeError(encoder, errors.New("wait_seconds must be between 0 and 5"))
		return
	}
	s.mu.RLock()
	state := s.results[command.ProcessID]
	s.mu.RUnlock()
	if state == nil {
		writeError(encoder, errors.New("process result expired or not found"))
		return
	}
	deadline := time.NewTimer(time.Duration(command.WaitSeconds * float64(time.Second)))
	defer deadline.Stop()
	for {
		state.mu.Lock()
		if !state.running && time.Since(state.finishedAt) >= processResultRetention {
			state.mu.Unlock()
			writeError(encoder, errors.New("process result expired or not found"))
			return
		}
		if !state.running || command.WaitSeconds == 0 {
			message := response{Version: protocolVersion, Type: "result", PID: state.pid, ProcessID: state.id, Running: state.running, ExitCode: state.exitCode}
			message.Stdout, message.Stderr = string(state.stdout), string(state.stderr)
			message.StdoutTruncated, message.StderrTruncated = state.stdoutTruncated, state.stderrTruncated
			if !state.running {
				expiresAt := state.finishedAt.Add(processResultRetention)
				message.ExpiresAt = &expiresAt
			}
			state.mu.Unlock()
			_ = encoder.Encode(message)
			return
		}
		changed := state.changed
		state.mu.Unlock()
		select {
		case <-changed:
		case <-deadline.C:
			command.WaitSeconds = 0
		}
	}
}

// startChild registers the process before the reaper can observe it, so its
// exit status stays with the caller's Wait.
func (s *supervisor) startChild(command *exec.Cmd) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := command.Start(); err != nil {
		return err
	}
	s.directChildren[command.Process.Pid] = struct{}{}
	return nil
}

// waitChild calls exited once the process has exited but before it is reaped,
// while its pid cannot yet be reused, then reaps it.
func (s *supervisor) waitChild(command *exec.Cmd, exited func()) error {
	awaitExit(command.Process.Pid)
	exited()
	err := command.Wait()
	s.mu.Lock()
	delete(s.directChildren, command.Process.Pid)
	s.mu.Unlock()
	return err
}

func (s *supervisor) isDirectChild(pid int) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	_, ok := s.directChildren[pid]
	return ok
}

func (s *supervisor) handleWatch(decoder *json.Decoder, encoder *json.Encoder, id string, ackSeq uint64) {
	s.mu.RLock()
	state := s.results[id]
	s.mu.RUnlock()
	if state == nil {
		_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "process not found"})
		return
	}
	s.streamProcess(decoder, encoder, state, ackSeq)
}

func (s *supervisor) streamProcess(decoder *json.Decoder, encoder *json.Encoder, state *processState, ackSeq uint64) {
	state.mu.Lock()
	state.readers++
	state.mu.Unlock()
	defer func() {
		state.mu.Lock()
		state.readers--
		state.mu.Unlock()
	}()
	for {
		state.mu.Lock()
		if ackSeq > state.ackSeq {
			state.ackSeq = ackSeq
		}
		for len(state.logs) > 0 && state.logs[0].Seq <= state.ackSeq {
			state.pendingBytes -= len(state.logs[0].Data)
			state.releaseBytes(len(state.logs[0].Data))
			state.logs[0] = logChunk{}
			state.logs = state.logs[1:]
		}
		if (len(state.logs) > 0 && state.ackSeq+1 < state.logs[0].Seq) || (len(state.logs) == 0 && state.ackSeq+1 < state.nextSeq) {
			state.mu.Unlock()
			_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "log replay window exceeded"})
			return
		}
		var next *logChunk
		for index := range state.logs {
			if state.logs[index].Seq > state.ackSeq {
				chunk := state.logs[index]
				next = &chunk
				break
			}
		}
		running := state.running
		exitCode := state.exitCode
		changed := state.changed
		state.mu.Unlock()
		if next != nil {
			if err := encoder.Encode(response{Version: protocolVersion, Type: "chunk", PID: state.pid, Seq: next.Seq, Stream: next.Stream, Data: next.Data}); err != nil {
				return
			}
			var ack request
			if err := decoder.Decode(&ack); err != nil {
				return
			}
			if ack.Version != protocolVersion || ack.Op != "ack" || ack.PID != state.pid || ack.AckSeq != next.Seq {
				return
			}
			if ack.OK {
				ackSeq = ack.AckSeq
			}
			continue
		}
		if !running {
			_ = encoder.Encode(response{Version: protocolVersion, Type: "exited", PID: state.pid, ExitCode: exitCode})
			return
		}
		<-changed
	}
}

func (s *supervisor) handleStatus(encoder *json.Encoder, pid int) {
	state := s.process(pid)
	if state == nil {
		_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "process not found"})
		return
	}
	state.mu.Lock()
	message := response{Version: protocolVersion, Type: "status", PID: pid, Running: state.running, ExitCode: state.exitCode}
	state.mu.Unlock()
	_ = encoder.Encode(message)
}

func (s *supervisor) handleOutput(encoder *json.Encoder, pid int, stream string) {
	state := s.process(pid)
	if state == nil {
		_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "process not found"})
		return
	}
	state.mu.Lock()
	message := response{Version: protocolVersion, Type: stream, PID: pid}
	if stream == "stdout" {
		message.Stdout = string(state.stdout)
	} else {
		message.Stderr = string(state.stderr)
	}
	state.mu.Unlock()
	_ = encoder.Encode(message)
}

func (s *supervisor) handleList(encoder *json.Encoder) {
	s.mu.RLock()
	states := make([]*processState, 0, len(s.processes))
	for _, state := range s.processes {
		states = append(states, state)
	}
	s.mu.RUnlock()
	views := make([]processView, 0, len(states))
	for _, state := range states {
		state.mu.Lock()
		views = append(views, processView{PID: state.pid, ProcessID: state.id, Command: state.command, Cwd: state.cwd, Running: state.running, ExitCode: state.exitCode})
		state.mu.Unlock()
	}
	sort.Slice(views, func(i, j int) bool { return views[i].PID < views[j].PID })
	_ = encoder.Encode(response{Version: protocolVersion, Type: "processes", Processes: views})
}

func (s *supervisor) handleKill(encoder *json.Encoder, pid int, id string, signalNumber int) {
	state := s.process(pid)
	if state == nil || (id != "" && state.id != id) {
		_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "process not found"})
		return
	}
	if signalNumber == 0 {
		signalNumber = int(syscall.SIGTERM)
	}
	state.mu.Lock()
	var err error
	if state.alive {
		err = syscall.Kill(-pid, syscall.Signal(signalNumber))
	}
	state.mu.Unlock()
	if err != nil && !errors.Is(err, syscall.ESRCH) {
		writeError(encoder, err)
		return
	}
	_ = encoder.Encode(response{Version: protocolVersion, Type: "killed", PID: pid})
}

func (s *supervisor) process(pid int) *processState {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.processes[pid]
}

func writeError(encoder *json.Encoder, err error) {
	_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: err.Error()})
}

func fatal(err error) {
	_, _ = fmt.Fprintln(os.Stderr, err)
	os.Exit(1)
}
