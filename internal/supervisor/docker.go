package supervisor

import (
	"context"
	"fmt"
	"net"
	"os"
	"os/exec"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

const (
	dockerSocket = "/var/run/docker.sock"
	// dockerStartTimeout bounds the wait for the daemon's socket before the
	// workload starts.
	dockerStartTimeout = time.Minute
	dockerStopGrace    = 10 * time.Second
	dockerMaxBackoff   = 30 * time.Second
)

// dockerdArgs run a daemon inside the container without touching the
// container's network or needing a storage driver beyond the filesystem.
var dockerdArgs = []string{ //nolint:gochecknoglobals // constant command line
	"dockerd", "--iptables=false", "--ip6tables=false", "--bridge=none", "--storage-driver=vfs", "--userland-proxy=false",
}

// dockerDaemon keeps dockerd running in the background until stopped,
// restarting it with backoff when it exits.
type dockerDaemon struct {
	s      *Supervisor
	cancel context.CancelFunc
	done   sync.WaitGroup
}

// startDocker starts dockerd and waits until its socket accepts
// connections.
func (s *Supervisor) startDocker(ctx context.Context) (*dockerDaemon, error) {
	runCtx, cancel := context.WithCancel(ctx)
	d := &dockerDaemon{s: s, cancel: cancel}
	started := make(chan error, 1)
	d.done.Go(func() { d.supervise(runCtx, started) })
	if err := <-started; err != nil {
		d.stop()
		return nil, err
	}
	waitCtx, stopWait := context.WithTimeout(runCtx, dockerStartTimeout)
	defer stopWait()
	for {
		conn, err := (&net.Dialer{}).DialContext(waitCtx, "unix", dockerSocket)
		if err == nil {
			_ = conn.Close()
			s.log.Info("docker daemon ready")
			return d, nil
		}
		select {
		case <-waitCtx.Done():
			d.stop()
			return nil, fmt.Errorf("the docker daemon did not answer on %s: %w", dockerSocket, err)
		case <-time.After(readinessInterval):
		}
	}
}

// supervise runs dockerd until ctx ends. The first start's result goes to
// started.
func (d *dockerDaemon) supervise(ctx context.Context, started chan<- error) {
	backoff := time.Second
	for first := true; ; first = false {
		cmd := exec.Command(dockerdArgs[0], dockerdArgs[1:]...) //nolint:gosec,noctx // stopped through its group
		cmd.Env = containerEnvironment()
		// The daemon's log goes to the container's log, not task output.
		cmd.Stdout, cmd.Stderr = os.Stderr, os.Stderr
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		err := d.s.children.start(cmd)
		if first {
			started <- err
		}
		if err != nil {
			if first {
				return
			}
			d.s.log.Error("docker daemon did not start", "error", err)
		} else {
			leader := &groupLeader{pid: cmd.Process.Pid}
			exited := make(chan error, 1)
			go func() { exited <- d.s.children.wait(cmd, leader.markExited) }()
			ranAt := time.Now()
			select {
			case err = <-exited:
				d.s.log.Warn("docker daemon exited", "code", exitCode(err))
				if time.Since(ranAt) > dockerMaxBackoff {
					backoff = time.Second
				}
			case <-ctx.Done():
				leader.signalGroup(unix.SIGTERM)
				kill := time.AfterFunc(dockerStopGrace, func() { leader.signalGroup(unix.SIGKILL) })
				<-exited
				kill.Stop()
				return
			}
		}
		if !sleepCtx(ctx, backoff) {
			return
		}
		backoff = min(backoff*2, dockerMaxBackoff)
	}
}

// stop terminates the daemon and waits for it.
func (d *dockerDaemon) stop() {
	d.cancel()
	d.done.Wait()
}
