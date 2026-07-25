package main

import (
	"crypto/subtle"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/creack/pty"
)

const (
	defaultShellPort          = 2222
	defaultShellIdleTimeout   = 30 * time.Second
	shellAuthTimeout          = 10 * time.Second
	shellMaxFramePayloadBytes = 1024 * 1024
	shellDefaultTerm          = "xterm-256color"
	shellDefaultColumns       = 80
	shellDefaultRows          = 24
)

const (
	shellFrameAuth   byte = 'A'
	shellFrameData   byte = 'D'
	shellFrameResize byte = 'R'
	shellFrameReady  byte = 'O'
	shellFrameError  byte = 'E'
	shellFrameExit   byte = 'X'
)

type shellAuth struct {
	Username string `json:"username"`
	Password string `json:"password"`
	Term     string `json:"term"`
	Columns  int    `json:"cols"`
	Rows     int    `json:"rows"`
	Probe    bool   `json:"probe,omitempty"`
}

type shellResize struct {
	Columns int `json:"cols"`
	Rows    int `json:"rows"`
}

type lockedShellWriter struct {
	mu         sync.Mutex
	connection net.Conn
}

type shellIdleLifecycle struct {
	mu             sync.Mutex
	idleTimeout    time.Duration
	idleDeadline   time.Time
	accepting      bool
	pendingAuth    map[net.Conn]struct{}
	activeSessions int
	changed        chan struct{}
	stopped        bool
}

func newShellIdleLifecycle(idleTimeout time.Duration) *shellIdleLifecycle {
	return &shellIdleLifecycle{
		idleTimeout:  idleTimeout,
		idleDeadline: time.Now().Add(idleTimeout),
		accepting:    true,
		pendingAuth:  make(map[net.Conn]struct{}),
		changed:      make(chan struct{}),
	}
}

func (l *shellIdleLifecycle) beginAuthentication(connection net.Conn) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.stopped || (!l.accepting && l.activeSessions == 0) {
		return false
	}
	l.pendingAuth[connection] = struct{}{}
	l.notifyLocked()
	return true
}

func (l *shellIdleLifecycle) finishAuthentication(connection net.Conn, probe bool) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.pendingAuth, connection)
	if l.stopped || (!l.accepting && l.activeSessions == 0) {
		l.notifyLocked()
		return false
	}
	if !probe {
		l.activeSessions++
		l.accepting = true
	}
	l.notifyLocked()
	return true
}

func (l *shellIdleLifecycle) failAuthentication(connection net.Conn) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.pendingAuth, connection)
	l.notifyLocked()
}

func (l *shellIdleLifecycle) finishSession() {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.activeSessions--
	if l.activeSessions == 0 {
		l.idleDeadline = time.Now().Add(l.idleTimeout)
		l.accepting = true
	}
	l.notifyLocked()
}

func (l *shellIdleLifecycle) stop() {
	l.mu.Lock()
	if l.stopped {
		l.mu.Unlock()
		return
	}
	l.stopped = true
	pending := l.pendingConnectionsLocked()
	l.notifyLocked()
	l.mu.Unlock()
	closeShellConnections(pending)
}

func (l *shellIdleLifecycle) waitForExpiration() bool {
	for {
		l.mu.Lock()
		if l.stopped {
			l.mu.Unlock()
			return false
		}
		changed := l.changed
		switch {
		case l.activeSessions > 0:
			l.mu.Unlock()
			<-changed
		case !l.accepting:
			l.mu.Unlock()
			return true
		default:
			remaining := time.Until(l.idleDeadline)
			if remaining <= 0 {
				l.accepting = false
				pending := l.pendingConnectionsLocked()
				l.notifyLocked()
				l.mu.Unlock()
				closeShellConnections(pending)
				return true
			}
			l.mu.Unlock()
			timer := time.NewTimer(remaining)
			select {
			case <-timer.C:
			case <-changed:
				if !timer.Stop() {
					<-timer.C
				}
			}
		}
	}
}

func (l *shellIdleLifecycle) notifyLocked() {
	close(l.changed)
	l.changed = make(chan struct{})
}

func (l *shellIdleLifecycle) pendingConnectionsLocked() []net.Conn {
	connections := make([]net.Conn, 0, len(l.pendingAuth))
	for connection := range l.pendingAuth {
		connections = append(connections, connection)
	}
	return connections
}

func closeShellConnections(connections []net.Conn) {
	for _, connection := range connections {
		_ = connection.Close()
	}
}

func (w *lockedShellWriter) frame(frameType byte, payload []byte) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	return writeShellFrame(w.connection, frameType, payload)
}

func runShell(args []string) error {
	flags := flag.NewFlagSet("shell", flag.ContinueOnError)
	port := flags.Int("port", defaultShellPort, "TCP port for the shell server")
	probe := flags.Bool("probe", false, "wait for a local shell listener")
	probeTimeout := flags.Duration("timeout", 5*time.Second, "listener probe timeout")
	idleTimeout := flags.Duration(
		"idle-timeout",
		defaultShellIdleTimeout,
		"exit after this long without an authenticated shell session",
	)
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *port < 1 || *port > 65535 {
		return fmt.Errorf("invalid shell port %d", *port)
	}
	if *idleTimeout <= 0 {
		return errors.New("shell idle timeout must be greater than zero")
	}
	username := os.Getenv("USERNAME")
	password := os.Getenv("PASSWORD")
	if username == "" || password == "" {
		return errors.New("shell server requires USERNAME and PASSWORD")
	}
	if *probe {
		return probeShellListener(*port, *probeTimeout, username, password)
	}
	listener, err := net.Listen("tcp", net.JoinHostPort("0.0.0.0", strconv.Itoa(*port)))
	if err != nil {
		var operationError *net.OpError
		if errors.As(err, &operationError) && errors.Is(operationError.Err, syscall.EADDRINUSE) {
			return nil
		}
		return fmt.Errorf("listen for shell connections: %w", err)
	}
	return serveShellListener(listener, username, password, *idleTimeout)
}

func serveShellListener(
	listener net.Listener,
	username string,
	password string,
	idleTimeout time.Duration,
) error {
	lifecycle := newShellIdleLifecycle(idleTimeout)
	defer lifecycle.stop()
	go func() {
		if lifecycle.waitForExpiration() {
			_ = listener.Close()
		}
	}()
	for {
		connection, acceptErr := listener.Accept()
		if acceptErr != nil {
			if errors.Is(acceptErr, net.ErrClosed) {
				return nil
			}
			continue
		}
		if !lifecycle.beginAuthentication(connection) {
			_ = connection.Close()
			continue
		}
		go handleShellConnection(connection, username, password, lifecycle)
	}
}

func probeShellListener(port int, timeout time.Duration, username string, password string) error {
	deadline := time.Now().Add(timeout)
	address := net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
	payload, err := json.Marshal(shellAuth{
		Username: username,
		Password: password,
		Term:     shellDefaultTerm,
		Columns:  shellDefaultColumns,
		Rows:     shellDefaultRows,
		Probe:    true,
	})
	if err != nil {
		return fmt.Errorf("encode shell readiness credentials: %w", err)
	}
	lastError := errors.New("listener unavailable")
	for {
		connection, dialErr := net.DialTimeout("tcp", address, 250*time.Millisecond)
		if dialErr == nil {
			_ = connection.SetDeadline(time.Now().Add(500 * time.Millisecond))
			writeErr := writeShellFrame(connection, shellFrameAuth, payload)
			frameType, framePayload, readErr := readShellFrame(connection)
			_ = connection.Close()
			if writeErr == nil && readErr == nil && frameType == shellFrameReady {
				return nil
			}
			switch {
			case writeErr != nil:
				lastError = writeErr
			case readErr != nil:
				lastError = readErr
			default:
				lastError = fmt.Errorf("listener rejected shell readiness: %s", string(framePayload))
			}
		} else {
			lastError = dialErr
		}
		if time.Now().After(deadline) {
			return fmt.Errorf(
				"shell listener %s was not ready before %s: %w",
				address,
				timeout,
				lastError,
			)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func handleShellConnection(
	connection net.Conn,
	username string,
	password string,
	lifecycle *shellIdleLifecycle,
) {
	defer connection.Close()
	authenticationPending := true
	defer func() {
		if authenticationPending {
			lifecycle.failAuthentication(connection)
		}
	}()
	_ = connection.SetReadDeadline(time.Now().Add(shellAuthTimeout))
	frameType, payload, err := readShellFrame(connection)
	if err != nil {
		_ = writeShellFrame(connection, shellFrameError, []byte(err.Error()))
		return
	}
	if frameType != shellFrameAuth {
		_ = writeShellFrame(connection, shellFrameError, []byte("first shell frame must authenticate"))
		return
	}
	var auth shellAuth
	if err := json.Unmarshal(payload, &auth); err != nil {
		_ = writeShellFrame(connection, shellFrameError, []byte("invalid shell authentication payload"))
		return
	}
	if !shellCredentialsMatch(auth, username, password) {
		_ = writeShellFrame(connection, shellFrameError, []byte("invalid shell credentials"))
		return
	}
	if auth.Probe {
		accepted := lifecycle.finishAuthentication(connection, true)
		authenticationPending = false
		if !accepted {
			return
		}
		_ = writeShellFrame(connection, shellFrameReady, nil)
		return
	}
	accepted := lifecycle.finishAuthentication(connection, false)
	authenticationPending = false
	if !accepted {
		return
	}
	defer lifecycle.finishSession()
	_ = connection.SetReadDeadline(time.Time{})
	if auth.Term == "" {
		auth.Term = shellDefaultTerm
	}
	if auth.Columns <= 0 {
		auth.Columns = shellDefaultColumns
	}
	if auth.Rows <= 0 {
		auth.Rows = shellDefaultRows
	}

	command := exec.Command(shellExecutable(), "-i")
	command.Dir = shellWorkingDirectory()
	command.Env = shellChildEnvironment(auth.Term)
	pseudoterminal, err := pty.StartWithSize(command, &pty.Winsize{
		Cols: uint16(auth.Columns),
		Rows: uint16(auth.Rows),
	})
	if err != nil {
		_ = writeShellFrame(connection, shellFrameError, []byte("failed to start interactive shell"))
		return
	}
	defer pseudoterminal.Close()

	writer := &lockedShellWriter{connection: connection}
	if err := writer.frame(shellFrameReady, nil); err != nil {
		terminateShellProcess(command)
		_, _ = command.Process.Wait()
		return
	}

	clientDone := make(chan struct{})
	processDone := make(chan int, 1)
	outputDone := make(chan struct{})
	go forwardShellInput(connection, pseudoterminal, command, clientDone)
	go func() {
		defer close(outputDone)
		forwardShellOutput(pseudoterminal, writer)
	}()
	go func() {
		processDone <- shellExitCode(command.Wait())
	}()

	select {
	case exitCode := <-processDone:
		select {
		case <-outputDone:
		case <-time.After(time.Second):
		}
		_ = writer.frame(shellFrameExit, []byte(strconv.Itoa(exitCode)))
	case <-clientDone:
		terminateShellProcess(command)
		select {
		case <-processDone:
		case <-time.After(time.Second):
			_ = command.Process.Kill()
			<-processDone
		}
	}
}

func shellCredentialsMatch(auth shellAuth, username string, password string) bool {
	providedUsername := []byte(auth.Username)
	expectedUsername := []byte(username)
	providedPassword := []byte(auth.Password)
	expectedPassword := []byte(password)
	return len(providedUsername) == len(expectedUsername) &&
		len(providedPassword) == len(expectedPassword) &&
		subtle.ConstantTimeCompare(providedUsername, expectedUsername) == 1 &&
		subtle.ConstantTimeCompare(providedPassword, expectedPassword) == 1
}

func forwardShellInput(connection net.Conn, pseudoterminal *os.File, command *exec.Cmd, done chan<- struct{}) {
	defer close(done)
	for {
		frameType, payload, err := readShellFrame(connection)
		if err != nil {
			return
		}
		switch frameType {
		case shellFrameData:
			if _, err := pseudoterminal.Write(payload); err != nil {
				return
			}
		case shellFrameResize:
			var resize shellResize
			if json.Unmarshal(payload, &resize) != nil || resize.Columns <= 0 || resize.Rows <= 0 {
				continue
			}
			_ = pty.Setsize(pseudoterminal, &pty.Winsize{
				Cols: uint16(resize.Columns),
				Rows: uint16(resize.Rows),
			})
			if command.Process != nil {
				_ = command.Process.Signal(syscall.SIGWINCH)
			}
		}
	}
}

func forwardShellOutput(pseudoterminal *os.File, writer *lockedShellWriter) {
	buffer := make([]byte, 32*1024)
	for {
		count, err := pseudoterminal.Read(buffer)
		if count > 0 && writer.frame(shellFrameData, buffer[:count]) != nil {
			return
		}
		if err != nil {
			return
		}
	}
}

func readShellFrame(reader io.Reader) (byte, []byte, error) {
	header := make([]byte, 5)
	if _, err := io.ReadFull(reader, header); err != nil {
		return 0, nil, err
	}
	payloadLength := binary.BigEndian.Uint32(header[1:])
	if payloadLength > shellMaxFramePayloadBytes {
		return 0, nil, errors.New("shell frame payload exceeds maximum size")
	}
	payload := make([]byte, payloadLength)
	if _, err := io.ReadFull(reader, payload); err != nil {
		return 0, nil, err
	}
	return header[0], payload, nil
}

func writeShellFrame(writer io.Writer, frameType byte, payload []byte) error {
	if len(payload) > shellMaxFramePayloadBytes {
		return errors.New("shell frame payload exceeds maximum size")
	}
	header := make([]byte, 5)
	header[0] = frameType
	binary.BigEndian.PutUint32(header[1:], uint32(len(payload)))
	if _, err := writer.Write(header); err != nil {
		return err
	}
	_, err := writer.Write(payload)
	return err
}

func shellExecutable() string {
	if configured := os.Getenv("SHELL"); configured != "" && executableFile(configured) {
		return configured
	}
	for _, candidate := range []string{"/bin/bash", "/bin/sh"} {
		if executableFile(candidate) {
			return candidate
		}
	}
	return "/bin/sh"
}

func executableFile(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir() && info.Mode()&0o111 != 0
}

func shellChildEnvironment(term string) []string {
	environment := make([]string, 0, len(os.Environ())+1)
	for _, item := range os.Environ() {
		key, _, _ := strings.Cut(item, "=")
		if key == "USERNAME" || key == "PASSWORD" || key == "TERM" {
			continue
		}
		environment = append(environment, item)
	}
	return append(environment, "TERM="+term)
}

func shellWorkingDirectory() string {
	for _, candidate := range []string{"/workspace", "/mnt/code", os.Getenv("HOME"), "/"} {
		if candidate == "" {
			continue
		}
		info, err := os.Stat(candidate)
		if err == nil && info.IsDir() {
			absolute, absoluteErr := filepath.Abs(candidate)
			if absoluteErr == nil {
				return absolute
			}
			return candidate
		}
	}
	return "/"
}

func terminateShellProcess(command *exec.Cmd) {
	if command.Process != nil {
		_ = command.Process.Signal(syscall.SIGHUP)
	}
}

func shellExitCode(err error) int {
	if err == nil {
		return 0
	}
	var exitError *exec.ExitError
	if errors.As(err, &exitError) {
		return exitError.ExitCode()
	}
	return 1
}
