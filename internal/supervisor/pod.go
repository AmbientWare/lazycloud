package supervisor

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"golang.org/x/sys/unix"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	readinessInterval = 100 * time.Millisecond
	readinessTimeout  = 2 * time.Second
)

// CommandExitedError reports that a pod's command ended on its own. The
// supervisor sent CommandExited and exits with Code.
type CommandExitedError struct {
	Code int
}

func (e *CommandExitedError) Error() string {
	return fmt.Sprintf("the pod command exited with code %d", e.Code)
}

// runPod runs the pod's command instead of runner slots until it exits on
// its own (CommandExitedError), the agent drains the container (nil) or ctx
// ends. An empty command idles until drained.
func (s *Supervisor) runPod(ctx context.Context, pod *hostproto.PodProcess) error {
	command := pod.GetCommand()
	if len(command) == 0 {
		s.podReady()
		select {
		case <-ctx.Done():
			return fmt.Errorf("idle pod: %w", ctx.Err())
		case <-s.drain:
			return nil
		}
	}
	out := &slot{sup: s, buf: make([]byte, readChunk)}
	var pipes []*outputPipe
	var writers []*os.File
	closeAll := func() {
		for _, w := range writers {
			_ = w.Close()
		}
		for _, p := range pipes {
			_ = p.file.Close()
		}
	}
	for _, stream := range []hostproto.LogStream{hostproto.LogStream_LOG_STREAM_STDOUT, hostproto.LogStream_LOG_STREAM_STDERR} {
		r, w, err := os.Pipe()
		if err != nil {
			closeAll()
			return s.podStartFailed(fmt.Errorf("create output pipe: %w", err))
		}
		writers = append(writers, w)
		pipe, err := newOutputPipe(r, stream)
		if err != nil {
			_ = r.Close()
			closeAll()
			return s.podStartFailed(err)
		}
		pipes = append(pipes, pipe)
	}
	cmd := exec.Command(command[0], command[1:]...) //nolint:gosec,noctx // the workload's command; its group is signalled explicitly
	cmd.Dir = pod.GetWorkingDirectory()
	cmd.Env = containerEnvironment()
	cmd.Stdout, cmd.Stderr = writers[0], writers[1]
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	err := s.children.start(cmd)
	for _, w := range writers {
		_ = w.Close()
	}
	writers = nil
	if err != nil {
		closeAll()
		return s.podStartFailed(fmt.Errorf("start %q: %w", command[0], err))
	}
	s.log.Info("pod command started", "command", command[0], "pid", cmd.Process.Pid)
	leader := &groupLeader{pid: cmd.Process.Pid}

	var readers sync.WaitGroup
	for _, pipe := range pipes {
		readers.Go(func() { out.readOutput(ctx, pipe) })
	}
	exited := make(chan int, 1)
	var waiter sync.WaitGroup
	waiter.Go(func() { exited <- exitCode(s.children.wait(cmd, leader.markExited)) })

	readyCtx, stopReady := context.WithCancel(ctx)
	var readiness sync.WaitGroup
	readiness.Go(func() {
		if waitReady(readyCtx, pod) {
			s.podReady()
		}
	})
	finish := func() {
		stopReady()
		readiness.Wait()
		waiter.Wait()
		s.finishPodOutput(out, pipes, &readers)
	}

	select {
	case code := <-exited:
		finish()
		s.log.Info("pod command exited", "code", code)
		s.out.push(&hostproto.SupervisorMessage{Body: &hostproto.SupervisorMessage_CommandExited{
			CommandExited: &hostproto.CommandExited{ExitCode: int32(code)}, //nolint:gosec // exit codes are small
		}})
		return &CommandExitedError{Code: code}
	case <-s.drain:
		leader.signalGroup(unix.SIGTERM)
		code := <-exited
		finish()
		s.log.Info("pod command stopped", "code", code)
		return nil
	case <-ctx.Done():
		leader.signalGroup(unix.SIGTERM)
		kill := time.AfterFunc(killGrace, func() { leader.signalGroup(unix.SIGKILL) })
		<-exited
		kill.Stop()
		finish()
		return fmt.Errorf("pod command: %w", ctx.Err())
	}
}

func (s *Supervisor) podStartFailed(err error) error {
	s.loadFailed(&hostproto.RunnerError{Type: "CommandStartError", Message: err.Error()})
	return ErrLoadFailed
}

// finishPodOutput lets output a descendant still writes drain briefly, then
// closes the pipes and emits what remains.
func (s *Supervisor) finishPodOutput(out *slot, pipes []*outputPipe, readers *sync.WaitGroup) {
	drained := make(chan struct{})
	go func() {
		readers.Wait()
		close(drained)
	}()
	select {
	case <-drained:
	case <-time.After(outputDrainGrace):
		for _, p := range pipes {
			_ = p.file.Close()
		}
		<-drained
	}
	out.outMu.Lock()
	for _, p := range pipes {
		out.emitLocked(p, nil, true)
	}
	out.outMu.Unlock()
	for _, p := range pipes {
		_ = p.file.Close()
	}
}

// podReady reports the pod's one slot ready, once.
func (s *Supervisor) podReady() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ready {
		return
	}
	s.ready, s.readySlots = true, 1
	s.log.Info("pod ready")
	s.out.push(&hostproto.SupervisorMessage{Body: &hostproto.SupervisorMessage_Ready{Ready: &hostproto.SlotsReady{Slots: 1}}})
}

// waitReady polls the pod's health check, or without one a TCP connection to
// its first port, until it answers or ctx ends. With neither the pod is ready
// once its command started.
func waitReady(ctx context.Context, pod *hostproto.PodProcess) bool {
	var check func(context.Context) bool
	switch health, ports := pod.GetHealth(), pod.GetPorts(); {
	case health != nil:
		path := health.GetPath()
		if !strings.HasPrefix(path, "/") {
			path = "/" + path
		}
		url := "http://" + net.JoinHostPort("127.0.0.1", strconv.Itoa(int(health.GetPort()))) + path
		client := &http.Client{
			Transport:     &http.Transport{DisableKeepAlives: true},
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		}
		defer client.CloseIdleConnections()
		check = func(ctx context.Context) bool {
			req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
			if err != nil {
				return false
			}
			resp, err := client.Do(req)
			if err != nil {
				return false
			}
			_ = resp.Body.Close()
			return resp.StatusCode >= 200 && resp.StatusCode < 400
		}
	case len(ports) > 0:
		address := net.JoinHostPort("127.0.0.1", strconv.Itoa(int(ports[0])))
		check = func(ctx context.Context) bool {
			conn, err := (&net.Dialer{}).DialContext(ctx, "tcp", address)
			if err != nil {
				return false
			}
			_ = conn.Close()
			return true
		}
	default:
		return ctx.Err() == nil
	}
	ticker := time.NewTicker(readinessInterval)
	defer ticker.Stop()
	for {
		attempt, cancel := context.WithTimeout(ctx, readinessTimeout)
		ok := check(attempt)
		cancel()
		if ok {
			return ctx.Err() == nil
		}
		select {
		case <-ctx.Done():
			return false
		case <-ticker.C:
		}
	}
}
