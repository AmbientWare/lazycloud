package supervisor

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"sync"

	"golang.org/x/sys/unix"
)

// children tracks the runner processes os/exec waits for, so the orphan
// reaper never takes their exit status.
type children struct {
	mu   sync.Mutex
	pids map[int]struct{}
}

func newChildren() *children {
	return &children{pids: make(map[int]struct{})}
}

// start registers the process before the reaper can observe it. The lock
// covers only fork and exec.
func (c *children) start(cmd *exec.Cmd) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("start process: %w", err)
	}
	c.pids[cmd.Process.Pid] = struct{}{}
	return nil
}

func (c *children) remove(pid int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	delete(c.pids, pid)
}

// reapOrphans waits for re-parented processes on every SIGCHLD. As PID 1 the
// supervisor inherits every orphan in the container.
func (c *children) reapOrphans(ctx context.Context) {
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, unix.SIGCHLD)
	defer signal.Stop(signals)
	for {
		select {
		case <-ctx.Done():
			return
		case <-signals:
			c.reapExited()
		}
	}
}

func (c *children) reapExited() {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return
	}
	self := os.Getpid()
	for _, entry := range entries {
		pid, err := strconv.Atoi(entry.Name())
		if err != nil || parentPID(entry.Name()) != self {
			continue
		}
		c.mu.Lock()
		if _, direct := c.pids[pid]; !direct {
			var status unix.WaitStatus
			_, _ = unix.Wait4(pid, &status, unix.WNOHANG, nil)
		}
		c.mu.Unlock()
	}
}

func parentPID(pid string) int {
	status, err := os.ReadFile("/proc/" + pid + "/status") //nolint:gosec // pid is a /proc entry name
	if err != nil {
		return 0
	}
	for line := range bytes.SplitSeq(status, []byte{'\n'}) {
		if value, ok := bytes.CutPrefix(line, []byte("PPid:")); ok {
			ppid, _ := strconv.Atoi(string(bytes.TrimSpace(value)))
			return ppid
		}
	}
	return 0
}

// awaitExit blocks until pid has exited without reaping it.
func awaitExit(pid int) {
	var info unix.Siginfo
	for {
		err := unix.Waitid(unix.P_PID, pid, &info, unix.WEXITED|unix.WNOWAIT, nil)
		if !errors.Is(err, unix.EINTR) {
			return
		}
	}
}
