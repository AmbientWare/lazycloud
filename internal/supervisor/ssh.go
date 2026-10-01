package supervisor

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/pkg/sftp"
	"golang.org/x/crypto/ssh"
	"golang.org/x/sys/unix"

	"github.com/AmbientWare/lazycloud/internal/apitypes" //nolint:depguard // the control API bodies are the public schemas; the rule denies internal/api by prefix
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	sshLoginUser        = "root"
	sshHandshakeTimeout = 30 * time.Second
	sshKeepAlive        = "keepalive@openssh.com"
	sshServerVersion    = "SSH-2.0-LazyCloud"
	forwardDialTimeout  = 10 * time.Second
)

var sshEnvName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`) //nolint:gochecknoglobals // constant pattern

// sshServerConfig accepts only certificates the workspace's user authority
// signed for root.
func sshServerConfig(identity *hostproto.SshServer) (*ssh.ServerConfig, error) {
	hostKey, err := ssh.ParsePrivateKey(identity.GetHostKey())
	if err != nil {
		return nil, fmt.Errorf("parse SSH host key: %w", err)
	}
	authority, _, _, _, err := ssh.ParseAuthorizedKey([]byte(identity.GetUserAuthority()))
	if err != nil {
		return nil, fmt.Errorf("parse SSH user authority: %w", err)
	}
	checker := &ssh.CertChecker{
		IsUserAuthority: func(key ssh.PublicKey) bool { return bytes.Equal(key.Marshal(), authority.Marshal()) },
	}
	config := &ssh.ServerConfig{
		ServerVersion: sshServerVersion,
		PublicKeyCallback: func(conn ssh.ConnMetadata, key ssh.PublicKey) (*ssh.Permissions, error) {
			if conn.User() != sshLoginUser {
				return nil, fmt.Errorf("ssh: user %q is not permitted", conn.User())
			}
			cert, ok := key.(*ssh.Certificate)
			if !ok {
				return nil, errors.New("ssh: a certificate from the workspace authority is required")
			}
			// The checker accepts an empty principal list for any user; the
			// workspace authority always names root.
			if !slices.Contains(cert.ValidPrincipals, sshLoginUser) {
				return nil, errors.New("ssh: the certificate does not name root")
			}
			perms, err := checker.Authenticate(conn, key)
			if err != nil {
				return nil, fmt.Errorf("ssh: %w", err)
			}
			return perms, nil
		},
	}
	config.AddHostKey(hostKey)
	return config, nil
}

func (c *control) openSSH(w http.ResponseWriter, r *http.Request) error {
	if c.ssh == nil {
		return apiErr(http.StatusNotFound, apitypes.NotFound, "this container serves no SSH")
	}
	conn, err := upgrade(w, r)
	if err != nil {
		return err
	}
	c.serveSSH(conn)
	return nil
}

// serveSSH serves one SSH connection until it ends or the control server
// closes, and waits for everything it started.
func (c *control) serveSSH(conn net.Conn) {
	defer func() { _ = conn.Close() }()
	_ = conn.SetDeadline(time.Now().Add(sshHandshakeTimeout))
	server, channels, requests, err := ssh.NewServerConn(conn, c.ssh)
	if err != nil {
		return
	}
	_ = conn.SetDeadline(time.Time{})
	stop := context.AfterFunc(c.ctx, func() { _ = server.Close() })
	defer stop()
	var wg sync.WaitGroup
	defer wg.Wait()
	defer func() { _ = server.Close() }()
	wg.Go(func() {
		for req := range requests {
			if req.WantReply {
				_ = req.Reply(req.Type == sshKeepAlive, nil)
			}
		}
	})
	for nc := range channels {
		switch nc.ChannelType() {
		case "session":
			channel, reqs, err := nc.Accept()
			if err != nil {
				continue
			}
			s := &sshSession{c: c, wg: &wg, channel: channel, conn: server}
			wg.Go(func() { s.serve(reqs) })
		case "direct-tcpip":
			wg.Go(func() { forwardLocal(c.ctx, nc) })
		default:
			_ = nc.Reject(ssh.UnknownChannelType, "unsupported channel type")
		}
	}
}

type sshSession struct {
	c       *control
	wg      *sync.WaitGroup
	channel ssh.Channel
	conn    *ssh.ServerConn

	mu      sync.Mutex
	env     []string
	pty     *sshPty
	proc    *groupLeader
	started bool
}

type sshPty struct {
	term       string
	cols, rows uint32
	master     *os.File
}

// serve handles the session's requests until the client closes it. Closing
// the terminal hangs up a PTY session; a session without one is not
// signalled, so work it left running in the background outlives the
// connection, until the container stops.
func (s *sshSession) serve(requests <-chan *ssh.Request) {
	defer s.closeTerminal()
	for req := range requests {
		ok := s.handle(req)
		if req.WantReply {
			_ = req.Reply(ok, nil)
		}
	}
}

func (s *sshSession) handle(req *ssh.Request) bool {
	switch req.Type {
	case "pty-req":
		var p struct {
			Term                      string
			Cols, Rows, Width, Height uint32
			Modes                     string
		}
		if ssh.Unmarshal(req.Payload, &p) != nil {
			return false
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		if s.started || s.pty != nil {
			return false
		}
		s.pty = &sshPty{term: p.Term, cols: p.Cols, rows: p.Rows}
		return true
	case "window-change":
		var p struct{ Cols, Rows, Width, Height uint32 }
		if ssh.Unmarshal(req.Payload, &p) != nil {
			return false
		}
		s.resize(p.Cols, p.Rows)
		return true
	case "env":
		var p struct{ Name, Value string }
		if ssh.Unmarshal(req.Payload, &p) != nil || !sshEnvName.MatchString(p.Name) {
			return false
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		if s.started {
			return false
		}
		s.env = append(s.env, p.Name+"="+p.Value)
		return true
	case "shell":
		return s.start("")
	case "exec":
		command, ok := sshString(req.Payload)
		return ok && s.start(command)
	case "subsystem":
		name, ok := sshString(req.Payload)
		return ok && name == "sftp" && s.startSFTP()
	case "signal":
		name, ok := sshString(req.Payload)
		sig, known := sshSignals[name]
		if !ok || !known {
			return false
		}
		s.mu.Lock()
		proc := s.proc
		s.mu.Unlock()
		if proc != nil {
			proc.signalGroup(sig)
		}
		return true
	case sshKeepAlive:
		return true
	}
	return false
}

func (s *sshSession) start(command string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.started {
		return false
	}
	s.started = true
	l := currentLogin()
	cmd := exec.Command(l.shell) //nolint:gosec,noctx // the login shell; its group is signalled explicitly
	if command == "" {
		cmd.Args = []string{loginArgv0(l.shell)}
	} else {
		cmd.Args = []string{l.shell, "-c", command}
	}
	cmd.Dir = "/"
	if usableDir(l.home) {
		cmd.Dir = l.home
	}
	extra := append(slices.Clone(s.env), "SSH_CONNECTION="+connectionDescription(s.conn))
	if s.pty != nil {
		term := s.pty.term
		if term == "" {
			term = defaultTerm
		}
		extra = append(extra, "TERM="+term)
	}
	cmd.Env = loginEnvironment(s.c.env, l, extra...)
	var err error
	if s.pty != nil {
		err = s.startTerminal(cmd)
	} else {
		err = s.startPipes(cmd)
	}
	if err != nil {
		_, _ = fmt.Fprintf(s.channel.Stderr(), "failed to start the session: %v\r\n", err)
		return false
	}
	return true
}

func (s *sshSession) startTerminal(cmd *exec.Cmd) error {
	t, err := openTerminal(uint16(s.pty.cols), uint16(s.pty.rows)) //nolint:gosec // terminal sizes fit
	if err != nil {
		return err
	}
	cmd.Env = append(cmd.Env, "SSH_TTY="+t.tty.Name())
	t.attach(cmd)
	err = s.c.children.start(cmd)
	_ = t.tty.Close()
	if err != nil {
		_ = t.master.Close()
		return err
	}
	s.pty.master = t.master
	output := make(chan struct{})
	s.wg.Go(func() {
		defer close(output)
		_, _ = io.Copy(s.channel, t.master)
	})
	s.wg.Go(func() { _, _ = io.Copy(t.master, s.channel) })
	s.finishing(cmd, func() {
		// The shell's children can keep the terminal open after it exits;
		// the session ends with the shell.
		select {
		case <-output:
		case <-time.After(hangupGrace):
		}
	})
	return nil
}

func (s *sshSession) startPipes(cmd *exec.Cmd) error {
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return fmt.Errorf("create stdin: %w", err)
	}
	var readers, writers []*os.File
	for range 2 {
		r, w, err := os.Pipe()
		if err != nil {
			for _, f := range append(readers, writers...) {
				_ = f.Close()
			}
			return fmt.Errorf("create output pipe: %w", err)
		}
		readers, writers = append(readers, r), append(writers, w)
	}
	cmd.Stdout, cmd.Stderr = writers[0], writers[1]
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	err = s.c.children.start(cmd)
	for _, w := range writers {
		_ = w.Close()
	}
	if err != nil {
		for _, r := range readers {
			_ = r.Close()
		}
		return err
	}
	var output sync.WaitGroup
	for i, dst := range []io.Writer{s.channel, s.channel.Stderr()} {
		output.Add(1)
		s.wg.Go(func() {
			defer output.Done()
			_, _ = io.Copy(dst, readers[i])
			_ = readers[i].Close()
		})
	}
	s.wg.Go(func() {
		_, _ = io.Copy(stdin, s.channel)
		_ = stdin.Close()
	})
	// A transfer's last bytes are still in the pipe when the process exits,
	// so the channel stays open until every writer is gone.
	s.finishing(cmd, output.Wait)
	return nil
}

// finishing reports cmd's exit once drain returns and closes the channel.
// The control server closing kills the session's process group.
func (s *sshSession) finishing(cmd *exec.Cmd, drain func()) {
	proc := &groupLeader{pid: cmd.Process.Pid}
	s.proc = proc
	stop := context.AfterFunc(s.c.ctx, func() { proc.signalGroup(unix.SIGKILL) })
	s.wg.Go(func() {
		err := s.c.children.wait(cmd, proc.markExited)
		stop()
		drain()
		var exitErr *exec.ExitError
		status, signaled := syscall.WaitStatus(0), false
		if errors.As(err, &exitErr) {
			status, _ = exitErr.Sys().(syscall.WaitStatus)
			signaled = status.Signaled()
		}
		switch {
		case signaled:
			s.sendExitSignal(status.Signal(), status.CoreDump())
		case err != nil && exitErr == nil:
			s.sendExitStatus(255)
		default:
			s.sendExitStatus(uint32(exitCode(err))) //nolint:gosec // exit codes are small
		}
		_ = s.channel.CloseWrite()
		_ = s.channel.Close()
		s.closeTerminal()
	})
}

func (s *sshSession) startSFTP() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.started {
		return false
	}
	s.started = true
	dir := "/"
	if l := currentLogin(); usableDir(l.home) {
		dir = l.home
	}
	server, err := sftp.NewServer(s.channel, sftp.WithServerWorkingDirectory(dir))
	if err != nil {
		return false
	}
	s.wg.Go(func() {
		status := uint32(0)
		if err := server.Serve(); err != nil && !errors.Is(err, io.EOF) {
			status = 1
		}
		_ = server.Close()
		s.sendExitStatus(status)
		_ = s.channel.Close()
	})
	return true
}

func (s *sshSession) sendExitStatus(status uint32) {
	payload := make([]byte, 4)
	binary.BigEndian.PutUint32(payload, status)
	_, _ = s.channel.SendRequest("exit-status", false, payload)
}

func (s *sshSession) sendExitSignal(sig syscall.Signal, core bool) {
	name := "KILL"
	for candidate, value := range sshSignals {
		if value == sig {
			name = candidate
			break
		}
	}
	payload := ssh.Marshal(struct {
		Signal     string
		CoreDumped bool
		Message    string
		Language   string
	}{Signal: name, CoreDumped: core})
	_, _ = s.channel.SendRequest("exit-signal", false, payload)
}

func (s *sshSession) resize(cols, rows uint32) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.pty == nil {
		return
	}
	s.pty.cols, s.pty.rows = cols, rows
	if s.pty.master != nil {
		resizeTerminal(s.pty.master, uint16(cols), uint16(rows)) //nolint:gosec // terminal sizes fit
	}
}

func (s *sshSession) closeTerminal() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.pty != nil && s.pty.master != nil {
		_ = s.pty.master.Close()
		s.pty.master = nil
	}
}

func connectionDescription(conn *ssh.ServerConn) string {
	remoteHost, remotePort, _ := net.SplitHostPort(conn.RemoteAddr().String())
	localHost, localPort, _ := net.SplitHostPort(conn.LocalAddr().String())
	return strings.Join([]string{remoteHost, remotePort, localHost, localPort}, " ")
}

// forwardLocal serves a direct-tcpip channel to an address of this
// container.
func forwardLocal(ctx context.Context, nc ssh.NewChannel) {
	var req struct {
		Host           string
		Port           uint32
		OriginatorHost string
		OriginatorPort uint32
	}
	if ssh.Unmarshal(nc.ExtraData(), &req) != nil || req.Port == 0 || req.Port > 65535 {
		_ = nc.Reject(ssh.ConnectionFailed, "invalid forwarding request")
		return
	}
	address, err := containerLocalAddress(ctx, req.Host)
	if err != nil {
		_ = nc.Reject(ssh.Prohibited, err.Error())
		return
	}
	dialCtx, cancel := context.WithTimeout(ctx, forwardDialTimeout)
	target, err := (&net.Dialer{}).DialContext(dialCtx, "tcp", net.JoinHostPort(address.String(), strconv.Itoa(int(req.Port))))
	cancel()
	if err != nil {
		_ = nc.Reject(ssh.ConnectionFailed, err.Error())
		return
	}
	channel, requests, err := nc.Accept()
	if err != nil {
		_ = target.Close()
		return
	}
	// The request stream ends when the client closes the channel or the
	// connection drops; closing the target then unblocks a copy a target
	// ignoring the half-close would hold.
	discarded := make(chan struct{})
	go func() {
		defer close(discarded)
		ssh.DiscardRequests(requests)
		_ = target.Close()
	}()
	splice(ctx, channel, target.(*net.TCPConn)) //nolint:forcetypeassert // a tcp dial returns a TCPConn
	<-discarded
}

// containerLocalAddress resolves a forwarding destination and refuses
// anything outside this container, so a session cannot reach other hosts on
// the platform network.
func containerLocalAddress(ctx context.Context, host string) (net.IP, error) {
	addrs, err := net.InterfaceAddrs()
	if err != nil {
		return nil, fmt.Errorf("list container addresses: %w", err)
	}
	var local []net.IP
	for _, a := range addrs {
		if n, ok := a.(*net.IPNet); ok {
			local = append(local, n.IP)
		}
	}
	var candidates []net.IP
	if ip := net.ParseIP(host); ip != nil {
		candidates = []net.IP{ip}
	} else {
		lookupCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		resolved, err := net.DefaultResolver.LookupIPAddr(lookupCtx, host)
		if err != nil {
			return nil, fmt.Errorf("resolve %s: %w", host, err)
		}
		for _, a := range resolved {
			candidates = append(candidates, a.IP)
		}
	}
	if len(candidates) == 0 {
		return nil, fmt.Errorf("%s has no address", host)
	}
	for _, ip := range candidates {
		if !ip.IsLoopback() && !slices.ContainsFunc(local, ip.Equal) {
			return nil, fmt.Errorf("forwarding to %s is not permitted; only this container's addresses are reachable", host)
		}
	}
	return candidates[0], nil
}

var sshSignals = map[string]unix.Signal{ //nolint:gochecknoglobals // constant table
	"ABRT": unix.SIGABRT, "ALRM": unix.SIGALRM, "FPE": unix.SIGFPE, "HUP": unix.SIGHUP, "ILL": unix.SIGILL,
	"INT": unix.SIGINT, "KILL": unix.SIGKILL, "PIPE": unix.SIGPIPE, "QUIT": unix.SIGQUIT, "SEGV": unix.SIGSEGV,
	"TERM": unix.SIGTERM, "USR1": unix.SIGUSR1, "USR2": unix.SIGUSR2,
}

func sshString(payload []byte) (string, bool) {
	var s struct{ Value string }
	if ssh.Unmarshal(payload, &s) != nil {
		return "", false
	}
	return s.Value, true
}
