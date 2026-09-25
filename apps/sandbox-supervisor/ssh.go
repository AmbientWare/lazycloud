package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/creack/pty"
	"github.com/pkg/sftp"
	"golang.org/x/crypto/ssh"
	"golang.org/x/sys/unix"
)

const (
	sshListenAddress       = "0.0.0.0:2223"
	sshHostKeyPath         = "/run/lazycloud/ssh/ssh_host_ed25519_key"
	sshUserCAPath          = "/run/lazycloud/ssh/user_ca.pub"
	sshLoginUser           = "root"
	sshHandshakeTimeout    = 30 * time.Second
	sshOutputDrainTimeout  = time.Second
	sshDefaultPath         = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
	sshDefaultTerm         = "xterm-256color"
	sshKeepAliveRequest    = "keepalive@openssh.com"
	sshServerVersionString = "SSH-2.0-LazyCloud"
)

var sshEnvironmentName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)

// childProcesses starts processes the adopted-child reaper must leave alone.
// An exit status the reaper takes is one the session can never report.
type childProcesses interface {
	startChild(command *exec.Cmd) error
	waitChild(command *exec.Cmd, exited func()) error
}

type sshServer struct {
	config    *ssh.ServerConfig
	processes childProcesses
	login     sshLogin
}

type sshLogin struct {
	user  string
	home  string
	shell string
}

func newSSHServer(processes childProcesses, hostKeyPEM []byte, userCALine []byte) (*sshServer, error) {
	hostKey, err := ssh.ParsePrivateKey(hostKeyPEM)
	if err != nil {
		return nil, fmt.Errorf("parse SSH host key: %w", err)
	}
	authority, _, _, _, err := ssh.ParseAuthorizedKey(userCALine)
	if err != nil {
		return nil, fmt.Errorf("parse SSH user certificate authority: %w", err)
	}
	checker := &ssh.CertChecker{
		IsUserAuthority: func(key ssh.PublicKey) bool {
			return bytes.Equal(key.Marshal(), authority.Marshal())
		},
	}
	config := &ssh.ServerConfig{
		ServerVersion: sshServerVersionString,
		PublicKeyCallback: func(conn ssh.ConnMetadata, key ssh.PublicKey) (*ssh.Permissions, error) {
			if conn.User() != sshLoginUser {
				return nil, fmt.Errorf("ssh: user %q is not permitted", conn.User())
			}
			certificate, ok := key.(*ssh.Certificate)
			if !ok {
				return nil, errors.New("ssh: a certificate from the workspace authority is required")
			}
			// An empty principal list is valid for any user in the checker; the
			// workspace authority always names the login user, so require it.
			if !slices.Contains(certificate.ValidPrincipals, sshLoginUser) {
				return nil, errors.New("ssh: certificate does not name the login user")
			}
			return checker.Authenticate(conn, key)
		},
	}
	config.AddHostKey(hostKey)
	return &sshServer{config: config, processes: processes, login: lookupLogin(sshLoginUser)}, nil
}

func startSSHServer(processes childProcesses) (net.Listener, error) {
	hostKey, err := os.ReadFile(sshHostKeyPath)
	if err != nil {
		return nil, fmt.Errorf("read SSH host key: %w", err)
	}
	userCA, err := os.ReadFile(sshUserCAPath)
	if err != nil {
		return nil, fmt.Errorf("read SSH user certificate authority: %w", err)
	}
	server, err := newSSHServer(processes, hostKey, userCA)
	if err != nil {
		return nil, err
	}
	listener, err := net.Listen("tcp", sshListenAddress)
	if err != nil {
		return nil, fmt.Errorf("listen for SSH connections: %w", err)
	}
	go server.serve(listener)
	return listener, nil
}

func (s *sshServer) serve(listener net.Listener) {
	for {
		connection, err := listener.Accept()
		if err != nil {
			if errors.Is(err, net.ErrClosed) {
				return
			}
			continue
		}
		go s.handleConnection(connection)
	}
}

func (s *sshServer) handleConnection(connection net.Conn) {
	defer connection.Close()
	_ = connection.SetDeadline(time.Now().Add(sshHandshakeTimeout))
	serverConnection, channels, requests, err := ssh.NewServerConn(connection, s.config)
	if err != nil {
		return
	}
	defer serverConnection.Close()
	_ = connection.SetDeadline(time.Time{})
	go replyGlobalRequests(requests)
	for newChannel := range channels {
		switch newChannel.ChannelType() {
		case "session":
			channel, channelRequests, acceptErr := newChannel.Accept()
			if acceptErr != nil {
				continue
			}
			session := &sshSession{server: s, channel: channel, connection: serverConnection}
			go session.serve(channelRequests)
		case "direct-tcpip":
			go s.forwardLocal(newChannel)
		default:
			_ = newChannel.Reject(ssh.UnknownChannelType, "unsupported channel type")
		}
	}
}

func replyGlobalRequests(requests <-chan *ssh.Request) {
	for request := range requests {
		if request.WantReply {
			_ = request.Reply(request.Type == sshKeepAliveRequest, nil)
		}
	}
}

type sshSession struct {
	server     *sshServer
	channel    ssh.Channel
	connection *ssh.ServerConn

	mu       sync.Mutex
	env      []string
	terminal *sshTerminal
	command  *exec.Cmd
	started  bool
}

type sshTerminal struct {
	term    string
	columns uint32
	rows    uint32
	master  *os.File
}

// serve handles the session's requests until the client closes it. Closing the
// terminal hangs up a PTY session's processes; a session without one is not
// signalled, so work it started in the background outlives the connection.
func (s *sshSession) serve(requests <-chan *ssh.Request) {
	defer s.closeTerminal()
	for request := range requests {
		ok := s.handleRequest(request)
		if request.WantReply {
			_ = request.Reply(ok, nil)
		}
	}
}

func (s *sshSession) handleRequest(request *ssh.Request) bool {
	switch request.Type {
	case "pty-req":
		term, columns, rows, ok := parsePtyRequest(request.Payload)
		if !ok {
			return false
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		if s.started || s.terminal != nil {
			return false
		}
		s.terminal = &sshTerminal{term: term, columns: columns, rows: rows}
		return true
	case "window-change":
		columns, rows, ok := parseWindowChange(request.Payload)
		if !ok {
			return false
		}
		s.resize(columns, rows)
		return true
	case "env":
		name, value, ok := parseEnvRequest(request.Payload)
		if !ok || !sshEnvironmentName.MatchString(name) {
			return false
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		if s.started {
			return false
		}
		s.env = append(s.env, name+"="+value)
		return true
	case "shell":
		return s.start("")
	case "exec":
		command, ok := parseString(request.Payload)
		if !ok {
			return false
		}
		return s.start(string(command))
	case "subsystem":
		name, ok := parseString(request.Payload)
		if !ok || string(name) != "sftp" {
			return false
		}
		return s.startSFTP()
	case "signal":
		name, ok := parseString(request.Payload)
		if !ok {
			return false
		}
		signal, known := sshSignals[string(name)]
		if !known {
			return false
		}
		s.signal(signal)
		return true
	case sshKeepAliveRequest:
		return true
	default:
		return false
	}
}

func (s *sshSession) start(command string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.started {
		return false
	}
	s.started = true
	login := s.server.login
	var cmd *exec.Cmd
	if command == "" {
		cmd = exec.Command(login.shell)
		cmd.Args = []string{"-" + filepath.Base(login.shell)}
	} else {
		cmd = exec.Command(login.shell, "-c", command)
	}
	cmd.Dir = workingDirectory(login.home)
	cmd.Env = s.environment()
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	var err error
	if s.terminal != nil {
		err = s.startWithTerminal(cmd)
	} else {
		err = s.startWithPipes(cmd)
	}
	if err != nil {
		_, _ = fmt.Fprintf(s.channel.Stderr(), "failed to start session: %v\r\n", err)
		return false
	}
	s.command = cmd
	return true
}

func (s *sshSession) startWithTerminal(cmd *exec.Cmd) error {
	master, tty, err := pty.Open()
	if err != nil {
		return err
	}
	defer tty.Close()
	_ = pty.Setsize(master, &pty.Winsize{Cols: uint16(s.terminal.columns), Rows: uint16(s.terminal.rows)})
	// Closing a blocking PTY waits for its reader; an idle login would keep
	// both the terminal and its processes alive after SSH disconnects.
	if err := syscall.SetNonblock(int(master.Fd()), true); err != nil {
		_ = master.Close()
		return err
	}
	cmd.Env = append(cmd.Env, "SSH_TTY="+tty.Name())
	cmd.Stdin, cmd.Stdout, cmd.Stderr = tty, tty, tty
	cmd.SysProcAttr.Setctty = true
	if err := s.server.processes.startChild(cmd); err != nil {
		_ = master.Close()
		return err
	}
	s.terminal.master = master
	output := make(chan struct{})
	go func() {
		_, _ = io.Copy(s.channel, master)
		close(output)
	}()
	go func() {
		_, _ = io.Copy(master, s.channel)
	}()
	go s.finish(cmd, func() {
		// The shell's children can keep the terminal open after it exits; a
		// session ends with its shell, so drain briefly and then hang up.
		select {
		case <-output:
		case <-time.After(sshOutputDrainTimeout):
		}
	})
	return nil
}

func (s *sshSession) startWithPipes(cmd *exec.Cmd) error {
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return err
	}
	stdoutReader, stdoutWriter, err := os.Pipe()
	if err != nil {
		return err
	}
	stderrReader, stderrWriter, err := os.Pipe()
	if err != nil {
		_ = stdoutReader.Close()
		_ = stdoutWriter.Close()
		return err
	}
	cmd.Stdout, cmd.Stderr = stdoutWriter, stderrWriter
	startErr := s.server.processes.startChild(cmd)
	_ = stdoutWriter.Close()
	_ = stderrWriter.Close()
	if startErr != nil {
		_ = stdoutReader.Close()
		_ = stderrReader.Close()
		return startErr
	}
	var output sync.WaitGroup
	output.Add(2)
	go func() {
		defer output.Done()
		_, _ = io.Copy(s.channel, stdoutReader)
		_ = stdoutReader.Close()
	}()
	go func() {
		defer output.Done()
		_, _ = io.Copy(s.channel.Stderr(), stderrReader)
		_ = stderrReader.Close()
	}()
	go func() {
		_, _ = io.Copy(stdin, s.channel)
		_ = stdin.Close()
	}()
	// A file transfer's last bytes are still in the pipe when the process
	// exits, so the channel stays open until every writer is gone.
	go s.finish(cmd, output.Wait)
	return nil
}

func (s *sshSession) startSFTP() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.started {
		return false
	}
	s.started = true
	server, err := sftp.NewServer(s.channel, sftp.WithServerWorkingDirectory(workingDirectory(s.server.login.home)))
	if err != nil {
		return false
	}
	go func() {
		status := uint32(0)
		if serveErr := server.Serve(); serveErr != nil && !errors.Is(serveErr, io.EOF) {
			status = 1
		}
		_ = server.Close()
		s.sendExitStatus(status)
		_ = s.channel.Close()
	}()
	return true
}

func (s *sshSession) finish(cmd *exec.Cmd, drain func()) {
	waitErr := s.server.processes.waitChild(cmd, s.forgetCommand)
	drain()
	var exitError *exec.ExitError
	if errors.As(waitErr, &exitError) {
		if status, ok := exitError.Sys().(syscall.WaitStatus); ok && status.Signaled() {
			s.sendExitSignal(status.Signal(), status.CoreDump())
		} else {
			s.sendExitStatus(uint32(exitError.ExitCode()))
		}
	} else if waitErr != nil {
		s.sendExitStatus(255)
	} else {
		s.sendExitStatus(0)
	}
	_ = s.channel.CloseWrite()
	_ = s.channel.Close()
	s.closeTerminal()
}

func (s *sshSession) sendExitStatus(status uint32) {
	payload := make([]byte, 4)
	binary.BigEndian.PutUint32(payload, status)
	_, _ = s.channel.SendRequest("exit-status", false, payload)
}

func (s *sshSession) sendExitSignal(signal syscall.Signal, coreDumped bool) {
	name := "KILL"
	for candidate, value := range sshSignals {
		if value == signal {
			name = candidate
			break
		}
	}
	payload := ssh.Marshal(struct {
		Signal     string
		CoreDumped bool
		Message    string
		Language   string
	}{Signal: name, CoreDumped: coreDumped})
	_, _ = s.channel.SendRequest("exit-signal", false, payload)
}

func (s *sshSession) resize(columns uint32, rows uint32) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.terminal == nil {
		return
	}
	s.terminal.columns, s.terminal.rows = columns, rows
	if s.terminal.master != nil {
		// File.Fd switches a polled descriptor back to blocking mode.
		// Keep resize from disabling interruption of the terminal reader.
		raw, err := s.terminal.master.SyscallConn()
		if err == nil {
			_ = raw.Control(func(fd uintptr) {
				_ = unix.IoctlSetWinsize(int(fd), unix.TIOCSWINSZ, &unix.Winsize{
					Col: uint16(columns), Row: uint16(rows),
				})
			})
		}
	}
}

func (s *sshSession) signal(signal syscall.Signal) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.command != nil && s.command.Process != nil {
		_ = syscall.Kill(-s.command.Process.Pid, signal)
	}
}

// forgetCommand runs before the session's process is reaped. The reap frees
// its group id for reuse, so a later signal request must not use it.
func (s *sshSession) forgetCommand() {
	s.mu.Lock()
	s.command = nil
	s.mu.Unlock()
}

func (s *sshSession) closeTerminal() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.terminal != nil && s.terminal.master != nil {
		_ = s.terminal.master.Close()
		s.terminal.master = nil
	}
}

func (s *sshSession) environment() []string {
	login := s.server.login
	values := make(map[string]string)
	order := make([]string, 0, len(os.Environ())+len(s.env)+8)
	set := func(item string) {
		name, value, ok := strings.Cut(item, "=")
		if !ok {
			return
		}
		if _, seen := values[name]; !seen {
			order = append(order, name)
		}
		values[name] = value
	}
	for _, item := range os.Environ() {
		set(item)
	}
	for _, item := range s.env {
		set(item)
	}
	set("HOME=" + login.home)
	set("USER=" + login.user)
	set("LOGNAME=" + login.user)
	set("SHELL=" + login.shell)
	if values["PATH"] == "" {
		set("PATH=" + sshDefaultPath)
	}
	if s.terminal != nil {
		term := s.terminal.term
		if term == "" {
			term = sshDefaultTerm
		}
		set("TERM=" + term)
	}
	set("SSH_CONNECTION=" + connectionDescription(s.connection))
	environment := make([]string, 0, len(order))
	for _, name := range order {
		environment = append(environment, name+"="+values[name])
	}
	return environment
}

func connectionDescription(connection *ssh.ServerConn) string {
	remoteHost, remotePort, _ := net.SplitHostPort(connection.RemoteAddr().String())
	localHost, localPort, _ := net.SplitHostPort(connection.LocalAddr().String())
	return strings.Join([]string{remoteHost, remotePort, localHost, localPort}, " ")
}

func (s *sshServer) forwardLocal(newChannel ssh.NewChannel) {
	var request struct {
		Host           string
		Port           uint32
		OriginatorHost string
		OriginatorPort uint32
	}
	if err := ssh.Unmarshal(newChannel.ExtraData(), &request); err != nil || request.Port == 0 || request.Port > 65535 {
		_ = newChannel.Reject(ssh.ConnectionFailed, "invalid forwarding request")
		return
	}
	address, err := containerLocalAddress(request.Host)
	if err != nil {
		_ = newChannel.Reject(ssh.Prohibited, err.Error())
		return
	}
	target, err := net.DialTimeout("tcp", net.JoinHostPort(address.String(), strconv.Itoa(int(request.Port))), 10*time.Second)
	if err != nil {
		_ = newChannel.Reject(ssh.ConnectionFailed, err.Error())
		return
	}
	channel, requests, err := newChannel.Accept()
	if err != nil {
		_ = target.Close()
		return
	}
	// The request stream ends when the client closes the channel or the
	// connection drops. Closing the target then unblocks the copy reading it,
	// which a target that ignores the client's half-close would hold forever.
	go func() {
		ssh.DiscardRequests(requests)
		_ = target.Close()
	}()
	var copies sync.WaitGroup
	copies.Add(2)
	go func() {
		defer copies.Done()
		_, _ = io.Copy(target, channel)
		if tcp, ok := target.(*net.TCPConn); ok {
			_ = tcp.CloseWrite()
		}
	}()
	go func() {
		defer copies.Done()
		_, _ = io.Copy(channel, target)
		_ = channel.CloseWrite()
	}()
	copies.Wait()
	_ = channel.Close()
	_ = target.Close()
}

// containerLocalAddress resolves a forwarding destination and refuses anything
// outside this container, so a session cannot be used to reach other hosts
// from inside the platform network.
func containerLocalAddress(host string) (net.IP, error) {
	local, err := localAddresses()
	if err != nil {
		return nil, err
	}
	var candidates []net.IP
	if parsed := net.ParseIP(host); parsed != nil {
		candidates = []net.IP{parsed}
	} else {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		resolved, lookupErr := net.DefaultResolver.LookupIPAddr(ctx, host)
		if lookupErr != nil {
			return nil, fmt.Errorf("resolve %s: %w", host, lookupErr)
		}
		for _, item := range resolved {
			candidates = append(candidates, item.IP)
		}
	}
	if len(candidates) == 0 {
		return nil, fmt.Errorf("%s has no address", host)
	}
	for _, candidate := range candidates {
		if !candidate.IsLoopback() && !slices.ContainsFunc(local, candidate.Equal) {
			return nil, fmt.Errorf("forwarding to %s is not permitted; only this container's addresses are reachable", host)
		}
	}
	return candidates[0], nil
}

func localAddresses() ([]net.IP, error) {
	addresses, err := net.InterfaceAddrs()
	if err != nil {
		return nil, fmt.Errorf("list container addresses: %w", err)
	}
	local := make([]net.IP, 0, len(addresses))
	for _, address := range addresses {
		if network, ok := address.(*net.IPNet); ok {
			local = append(local, network.IP)
		}
	}
	return local, nil
}

func lookupLogin(user string) sshLogin {
	login := sshLogin{user: user, home: "/root"}
	if passwd, err := os.Open("/etc/passwd"); err == nil {
		scanner := bufio.NewScanner(passwd)
		for scanner.Scan() {
			fields := strings.Split(scanner.Text(), ":")
			if len(fields) >= 7 && fields[0] == user {
				if fields[5] != "" {
					login.home = fields[5]
				}
				if executableFile(fields[6]) {
					login.shell = fields[6]
				}
				break
			}
		}
		_ = passwd.Close()
	}
	if login.shell == "" {
		for _, candidate := range []string{"/bin/bash", "/bin/sh"} {
			if executableFile(candidate) {
				login.shell = candidate
				break
			}
		}
	}
	if login.shell == "" {
		login.shell = "/bin/sh"
	}
	return login
}

func workingDirectory(home string) string {
	if info, err := os.Stat(home); err == nil && info.IsDir() && syscall.Access(home, 0x1) == nil {
		return home
	}
	return "/"
}

var sshSignals = map[string]syscall.Signal{
	"ABRT": syscall.SIGABRT,
	"ALRM": syscall.SIGALRM,
	"FPE":  syscall.SIGFPE,
	"HUP":  syscall.SIGHUP,
	"ILL":  syscall.SIGILL,
	"INT":  syscall.SIGINT,
	"KILL": syscall.SIGKILL,
	"PIPE": syscall.SIGPIPE,
	"QUIT": syscall.SIGQUIT,
	"SEGV": syscall.SIGSEGV,
	"TERM": syscall.SIGTERM,
	"USR1": syscall.SIGUSR1,
	"USR2": syscall.SIGUSR2,
}

func parseString(payload []byte) ([]byte, bool) {
	if len(payload) < 4 {
		return nil, false
	}
	length := binary.BigEndian.Uint32(payload)
	if uint64(len(payload)-4) < uint64(length) {
		return nil, false
	}
	return payload[4 : 4+length], true
}

func parsePtyRequest(payload []byte) (string, uint32, uint32, bool) {
	var request struct {
		Term    string
		Columns uint32
		Rows    uint32
		Width   uint32
		Height  uint32
		Modes   string
	}
	if err := ssh.Unmarshal(payload, &request); err != nil {
		return "", 0, 0, false
	}
	return request.Term, request.Columns, request.Rows, true
}

func parseWindowChange(payload []byte) (uint32, uint32, bool) {
	var request struct {
		Columns uint32
		Rows    uint32
		Width   uint32
		Height  uint32
	}
	if err := ssh.Unmarshal(payload, &request); err != nil {
		return 0, 0, false
	}
	return request.Columns, request.Rows, true
}

func parseEnvRequest(payload []byte) (string, string, bool) {
	var request struct {
		Name  string
		Value string
	}
	if err := ssh.Unmarshal(payload, &request); err != nil {
		return "", "", false
	}
	return request.Name, request.Value, true
}
