package main

import (
	"bufio"
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
	"syscall"
	"time"
)

const (
	protocolVersion          = 1
	listenAddress            = "0.0.0.0:7111"
	defaultTokenPath         = "/run/lazycloud/sandbox-supervisor.token"
	maxPendingLogBytes       = 1024 * 1024
	maxRetainedOutputBytes   = 256 * 1024
	maxRetainedExitedProcess = 64
)

type request struct {
	Version int      `json:"version"`
	Op      string   `json:"op"`
	Argv    []string `json:"argv,omitempty"`
	Cwd     string   `json:"cwd,omitempty"`
	Env     []string `json:"env,omitempty"`
	PID     int      `json:"pid,omitempty"`
	Signal  int      `json:"signal,omitempty"`
	AckSeq  uint64   `json:"ack_seq,omitempty"`
	OK      bool     `json:"ok,omitempty"`
	Token   string   `json:"token,omitempty"`
}

type response struct {
	Version   int           `json:"version"`
	Type      string        `json:"type"`
	Error     string        `json:"error,omitempty"`
	PID       int           `json:"pid,omitempty"`
	Seq       uint64        `json:"seq,omitempty"`
	Stream    string        `json:"stream,omitempty"`
	Data      []byte        `json:"data,omitempty"`
	ExitCode  int           `json:"exit_code,omitempty"`
	Running   bool          `json:"running,omitempty"`
	Processes []processView `json:"processes,omitempty"`
	Stdout    string        `json:"stdout,omitempty"`
	Stderr    string        `json:"stderr,omitempty"`
}

type processView struct {
	PID      int    `json:"pid"`
	Command  string `json:"command"`
	Cwd      string `json:"cwd"`
	Running  bool   `json:"running"`
	ExitCode int    `json:"exit_code"`
}

type logChunk struct {
	Seq    uint64
	Stream string
	Data   []byte
}

type processState struct {
	mu           sync.Mutex
	pid          int
	command      string
	cwd          string
	running      bool
	exitCode     int
	nextSeq      uint64
	ackSeq       uint64
	logs         []logChunk
	changed      chan struct{}
	process      *os.Process
	pendingBytes int
	stdout       []byte
	stderr       []byte
	finishedAt   time.Time
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
}

func main() {
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
	if _, err := s.controlToken(); err != nil {
		fatal(err)
	}
	go s.reapAdoptedChildren()
	listener, err := net.Listen("tcp", listenAddress)
	if err != nil {
		fatal(err)
	}
	defer listener.Close()
	go s.handleSignals(listener)
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

func (s *supervisor) handleSignals(listener net.Listener) {
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGINT, syscall.SIGTERM)
	received := <-signals
	_ = listener.Close()
	signalNumber := received.(syscall.Signal)
	s.mu.RLock()
	processes := make([]*processState, 0, len(s.processes))
	for _, process := range s.processes {
		processes = append(processes, process)
	}
	s.mu.RUnlock()
	for _, process := range processes {
		process.mu.Lock()
		if process.running && process.process != nil {
			_ = syscall.Kill(-process.pid, signalNumber)
		}
		process.mu.Unlock()
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
	case "drain":
		s.handleDrain(encoder, connection)
	case "exec":
		s.handleExec(decoder, encoder, command)
	case "watch":
		s.handleWatch(decoder, encoder, command.PID, command.AckSeq)
	case "status":
		s.handleStatus(encoder, command.PID)
	case "stdout", "stderr":
		s.handleOutput(encoder, command.PID, command.Op)
	case "list":
		s.handleList(encoder)
	case "kill":
		s.handleKill(encoder, command.PID, command.Signal)
	default:
		_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "unknown operation"})
	}
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
	cmd := exec.Command(command.Argv[0], command.Argv[1:]...)
	cmd.Dir = command.Cwd
	cmd.Env = command.Env
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		writeError(encoder, err)
		return
	}
	stderr, err := cmd.StderrPipe()
	if err != nil {
		writeError(encoder, err)
		return
	}
	if err = cmd.Start(); err != nil {
		writeError(encoder, err)
		return
	}
	state := &processState{
		pid:      cmd.Process.Pid,
		command:  strings.Join(command.Argv, " "),
		cwd:      command.Cwd,
		running:  true,
		exitCode: -1,
		nextSeq:  1,
		changed:  make(chan struct{}),
		process:  cmd.Process,
	}
	s.mu.Lock()
	s.processes[state.pid] = state
	s.directChildren[state.pid] = struct{}{}
	s.mu.Unlock()
	var outputReaders sync.WaitGroup
	outputReaders.Add(2)
	go captureOutput(state, "stdout", stdout, &outputReaders)
	go captureOutput(state, "stderr", stderr, &outputReaders)
	go s.waitProcess(state, cmd, &outputReaders)
	if err := encoder.Encode(response{Version: protocolVersion, Type: "started", PID: state.pid, Running: true}); err != nil {
		return
	}
	s.streamProcess(decoder, encoder, state, 0)
}

func captureOutput(state *processState, stream string, reader io.Reader, done *sync.WaitGroup) {
	defer done.Done()
	buffer := make([]byte, 32*1024)
	for {
		count, err := reader.Read(buffer)
		if count > 0 {
			state.mu.Lock()
			data := append([]byte(nil), buffer[:count]...)
			state.logs = append(state.logs, logChunk{Seq: state.nextSeq, Stream: stream, Data: data})
			state.pendingBytes += len(data)
			for state.pendingBytes > maxPendingLogBytes && len(state.logs) > 0 {
				state.pendingBytes -= len(state.logs[0].Data)
				state.logs[0] = logChunk{}
				state.logs = state.logs[1:]
			}
			if stream == "stdout" {
				state.stdout = appendBounded(state.stdout, data, maxRetainedOutputBytes)
			} else {
				state.stderr = appendBounded(state.stderr, data, maxRetainedOutputBytes)
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

func (s *supervisor) waitProcess(state *processState, cmd *exec.Cmd, outputReaders *sync.WaitGroup) {
	err := cmd.Wait()
	s.mu.Lock()
	delete(s.directChildren, state.pid)
	s.mu.Unlock()
	outputReaders.Wait()
	exitCode := 0
	if err != nil {
		var exitError *exec.ExitError
		if errors.As(err, &exitError) {
			exitCode = exitError.ExitCode()
			if status, ok := exitError.ProcessState.Sys().(syscall.WaitStatus); ok && status.Signaled() {
				exitCode = 128 + int(status.Signal())
			}
		} else {
			exitCode = 1
		}
	}
	state.mu.Lock()
	state.running = false
	state.exitCode = exitCode
	state.finishedAt = time.Now()
	state.notify()
	state.mu.Unlock()
	s.pruneExitedProcesses()
}

func (s *supervisor) pruneExitedProcesses() {
	s.mu.Lock()
	defer s.mu.Unlock()
	type exitedProcess struct {
		pid        int
		finishedAt time.Time
	}
	exited := make([]exitedProcess, 0)
	for pid, state := range s.processes {
		state.mu.Lock()
		if !state.running {
			exited = append(exited, exitedProcess{pid: pid, finishedAt: state.finishedAt})
		}
		state.mu.Unlock()
	}
	if len(exited) <= maxRetainedExitedProcess {
		return
	}
	sort.Slice(exited, func(i, j int) bool { return exited[i].finishedAt.Before(exited[j].finishedAt) })
	for _, item := range exited[:len(exited)-maxRetainedExitedProcess] {
		delete(s.processes, item.pid)
	}
}

func (s *supervisor) isDirectChild(pid int) bool {
	s.mu.RLock()
	defer s.mu.RUnlock()
	_, ok := s.directChildren[pid]
	return ok
}

func (s *supervisor) handleWatch(decoder *json.Decoder, encoder *json.Encoder, pid int, ackSeq uint64) {
	state := s.process(pid)
	if state == nil {
		_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "process not found"})
		return
	}
	s.streamProcess(decoder, encoder, state, ackSeq)
}

func (s *supervisor) streamProcess(decoder *json.Decoder, encoder *json.Encoder, state *processState, ackSeq uint64) {
	for {
		state.mu.Lock()
		if ackSeq > state.ackSeq {
			state.ackSeq = ackSeq
		}
		for len(state.logs) > 0 && state.logs[0].Seq <= state.ackSeq {
			state.pendingBytes -= len(state.logs[0].Data)
			state.logs[0] = logChunk{}
			state.logs = state.logs[1:]
		}
		if len(state.logs) > 0 && state.ackSeq+1 < state.logs[0].Seq {
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
	defer state.mu.Unlock()
	_ = encoder.Encode(response{Version: protocolVersion, Type: "status", PID: pid, Running: state.running, ExitCode: state.exitCode})
}

func (s *supervisor) handleOutput(encoder *json.Encoder, pid int, stream string) {
	state := s.process(pid)
	if state == nil {
		_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "process not found"})
		return
	}
	state.mu.Lock()
	defer state.mu.Unlock()
	message := response{Version: protocolVersion, Type: stream, PID: pid}
	if stream == "stdout" {
		message.Stdout = string(state.stdout)
	} else {
		message.Stderr = string(state.stderr)
	}
	_ = encoder.Encode(message)
}

func appendBounded(existing, data []byte, limit int) []byte {
	if len(data) >= limit {
		return append([]byte(nil), data[len(data)-limit:]...)
	}
	overflow := len(existing) + len(data) - limit
	if overflow > 0 {
		existing = append([]byte(nil), existing[overflow:]...)
	}
	return append(existing, data...)
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
		views = append(views, processView{PID: state.pid, Command: state.command, Cwd: state.cwd, Running: state.running, ExitCode: state.exitCode})
		state.mu.Unlock()
	}
	sort.Slice(views, func(i, j int) bool { return views[i].PID < views[j].PID })
	_ = encoder.Encode(response{Version: protocolVersion, Type: "processes", Processes: views})
}

func (s *supervisor) handleKill(encoder *json.Encoder, pid int, signalNumber int) {
	state := s.process(pid)
	if state == nil {
		_ = encoder.Encode(response{Version: protocolVersion, Type: "error", Error: "process not found"})
		return
	}
	if signalNumber == 0 {
		signalNumber = int(syscall.SIGTERM)
	}
	state.mu.Lock()
	err := syscall.Kill(-pid, syscall.Signal(signalNumber))
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
