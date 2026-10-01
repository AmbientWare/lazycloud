package supervisor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/coder/websocket"
	"github.com/creack/pty"
	"golang.org/x/sys/unix"
)

const (
	defaultTerm = "xterm-256color"
	// hangupGrace is how long a hung-up shell has before SIGKILL, and how
	// long a finished shell's output may keep draining.
	hangupGrace = time.Second
	// maxShellMessage bounds one WebSocket message from the client.
	maxShellMessage = 1 << 20
)

var termPattern = regexp.MustCompile(`^[A-Za-z0-9._+-]{1,64}$`) //nolint:gochecknoglobals // constant pattern

// shellMessage is a text message on the shell WebSocket.
type shellMessage struct {
	Type    string  `json:"type"`
	Cols    *int    `json:"cols,omitempty"`
	Rows    *int    `json:"rows,omitempty"`
	Code    *int    `json:"code,omitempty"`
	Message *string `json:"message,omitempty"`
}

// terminal is a PTY whose master stays non-blocking, so closing it
// interrupts a read in progress.
type terminal struct {
	master *os.File
	tty    *os.File
}

func openTerminal(cols, rows uint16) (*terminal, error) {
	master, tty, err := pty.Open()
	if err != nil {
		return nil, fmt.Errorf("open a terminal: %w", err)
	}
	// pty.Open switched the master to blocking mode through File.Fd; a
	// blocking read would hold the terminal and its session open after
	// the client leaves. Fd is not called again.
	if err := unix.SetNonblock(int(master.Fd()), true); err != nil { //nolint:gosec // descriptors fit in int
		_ = master.Close()
		_ = tty.Close()
		return nil, fmt.Errorf("make the terminal non-blocking: %w", err)
	}
	t := &terminal{master: master, tty: tty}
	resizeTerminal(master, cols, rows)
	return t, nil
}

// resizeTerminal sets the window size without File.Fd, which would switch the
// master back to blocking mode.
func resizeTerminal(master *os.File, cols, rows uint16) {
	raw, err := master.SyscallConn()
	if err != nil {
		return
	}
	_ = raw.Control(func(fd uintptr) {
		_ = unix.IoctlSetWinsize(int(fd), unix.TIOCSWINSZ, &unix.Winsize{Col: cols, Row: rows}) //nolint:gosec // descriptors fit in int
	})
}

// attach makes the terminal cmd's controlling terminal in a new session.
func (t *terminal) attach(cmd *exec.Cmd) {
	cmd.Stdin, cmd.Stdout, cmd.Stderr = t.tty, t.tty, t.tty
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true, Setctty: true}
}

// groupLeader is a started process group leader that may be signalled until
// it is reaped.
type groupLeader struct {
	mu     sync.Mutex
	pid    int
	exited bool
}

func (p *groupLeader) markExited() {
	p.mu.Lock()
	p.exited = true
	p.mu.Unlock()
}

// signalGroup signals the session's process group while its leader is
// unreaped, so the group id cannot have been reused.
func (p *groupLeader) signalGroup(sig unix.Signal) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !p.exited {
		_ = unix.Kill(-p.pid, sig)
	}
}

func shellSize(r *http.Request, name string, def int) (uint16, error) {
	raw := r.URL.Query().Get(name)
	if raw == "" {
		return uint16(def), nil //nolint:gosec // defaults are small
	}
	v, err := strconv.Atoi(raw)
	if err != nil || v < 1 || v > 1000 {
		return 0, invalid("%s must be an integer from 1 to 1000", name)
	}
	return uint16(v), nil //nolint:gosec // validated above
}

// openShell runs the login shell on a PTY and carries it over a WebSocket.
func (c *control) openShell(w http.ResponseWriter, r *http.Request) error {
	cols, err := shellSize(r, "cols", 80)
	if err != nil {
		return err
	}
	rows, err := shellSize(r, "rows", 24)
	if err != nil {
		return err
	}
	term := r.URL.Query().Get("term")
	if term == "" {
		term = defaultTerm
	}
	if !termPattern.MatchString(term) {
		return invalid("term must match %s", termPattern)
	}
	// The socket is reachable only by the agent, which checks the origin.
	conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{InsecureSkipVerify: true})
	if err != nil {
		// Accept answered the request.
		return nil //nolint:nilerr // see above
	}
	defer func() { _ = conn.CloseNow() }()
	conn.SetReadLimit(maxShellMessage)
	ctx := c.ctx
	l := currentLogin()
	cmd := exec.Command(l.shell) //nolint:gosec,noctx // the login shell; its group is signalled explicitly
	cmd.Args = []string{loginArgv0(l.shell)}
	cmd.Dir = sessionDir(l)
	cmd.Env = loginEnvironment(c.env, l, "TERM="+term)
	t, err := openTerminal(cols, rows)
	if err == nil {
		t.attach(cmd)
		err = c.children.start(cmd)
		_ = t.tty.Close()
		if err != nil {
			_ = t.master.Close()
		}
	}
	if err != nil {
		message := err.Error()
		writeShellMessage(ctx, conn, shellMessage{Type: "error", Message: &message})
		_ = conn.Close(websocket.StatusInternalError, "the shell did not start")
		return nil
	}
	c.runShell(ctx, conn, t.master, cmd)
	return nil
}

func writeShellMessage(ctx context.Context, conn *websocket.Conn, m shellMessage) {
	data, err := json.Marshal(m)
	if err == nil {
		_ = conn.Write(ctx, websocket.MessageText, data)
	}
}

// runShell pumps the terminal until the shell exits or the client leaves.
func (c *control) runShell(ctx context.Context, conn *websocket.Conn, master *os.File, cmd *exec.Cmd) {
	proc := &groupLeader{pid: cmd.Process.Pid}
	var pumps sync.WaitGroup
	output := make(chan struct{})
	pumps.Go(func() {
		defer close(output)
		buf := make([]byte, readChunk)
		for {
			n, err := master.Read(buf)
			if n > 0 && conn.Write(ctx, websocket.MessageBinary, buf[:n]) != nil {
				return
			}
			if err != nil {
				return
			}
		}
	})
	gone := make(chan struct{})
	pumps.Go(func() {
		defer close(gone)
		for {
			kind, data, err := conn.Read(ctx)
			if err != nil {
				return
			}
			if kind == websocket.MessageBinary {
				if _, err := master.Write(data); err != nil && !errors.Is(err, os.ErrClosed) {
					return
				}
				continue
			}
			var m shellMessage
			if json.Unmarshal(data, &m) == nil && m.Type == "resize" && m.Cols != nil && m.Rows != nil &&
				*m.Cols > 0 && *m.Cols <= 1000 && *m.Rows > 0 && *m.Rows <= 1000 {
				resizeTerminal(master, uint16(*m.Cols), uint16(*m.Rows)) //nolint:gosec // validated above
			}
		}
	})
	exited := make(chan int, 1)
	pumps.Go(func() { exited <- exitCode(c.children.wait(cmd, proc.markExited)) })

	select {
	case code := <-exited:
		// Children of the shell can hold the terminal open; the session
		// ends with the shell.
		select {
		case <-output:
		case <-time.After(hangupGrace):
		}
		writeShellMessage(ctx, conn, shellMessage{Type: "exit", Code: &code})
		_ = conn.Close(websocket.StatusNormalClosure, "")
	case <-gone:
		proc.signalGroup(unix.SIGHUP)
		kill := time.AfterFunc(hangupGrace, func() { proc.signalGroup(unix.SIGKILL) })
		<-exited
		kill.Stop()
		_ = conn.CloseNow()
	}
	_ = master.Close()
	pumps.Wait()
}
