package agent

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"sync"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	// sessionQueue bounds messages waiting for the session stream. A full
	// queue means the server stopped reading; the agent reconnects and the
	// next Hello restates everything.
	sessionQueue      = 1024
	minSessionBackoff = 250 * time.Millisecond
	maxSessionBackoff = 10 * time.Second
)

// sessionOut is the outbound queue of one open session.
type sessionOut struct {
	ch     chan *hostproto.HostMessage
	cancel context.CancelFunc
}

// sessions keeps a session open, reconnecting with backoff, until ctx ends.
func (a *Agent) sessions(ctx context.Context) {
	delay := minSessionBackoff
	for ctx.Err() == nil {
		began := time.Now()
		err := a.runSession(ctx)
		if ctx.Err() != nil {
			return
		}
		if time.Since(began) > time.Minute {
			delay = minSessionBackoff
		}
		a.log.Warn("session ended", "error", err, "retry_in", delay)
		if !sleep(ctx, delay/2+rand.N(delay/2+1)) { //nolint:gosec // jitter needs no cryptographic randomness
			return
		}
		delay = min(2*delay, maxSessionBackoff)
	}
}

// runSession opens the stream with a Hello describing every container, then
// handles commands and sends reports until the stream fails.
func (a *Agent) runSession(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := a.control.Session(ctx)
	if err != nil {
		return fmt.Errorf("open session: %w", err)
	}
	out := &sessionOut{ch: make(chan *hostproto.HostMessage, sessionQueue), cancel: cancel}
	a.mu.Lock()
	a.session = out
	a.mu.Unlock()
	defer func() {
		a.mu.Lock()
		if a.session == out {
			a.session = nil
		}
		a.mu.Unlock()
	}()

	a.metrics.sessions.Inc()
	hello, exited := a.hello()
	if err := stream.Send(hello); err != nil {
		return fmt.Errorf("send hello: %w", err)
	}
	for _, c := range exited {
		a.goOwned(c.cleanup)
	}
	a.log.Info("session open", "containers", len(hello.GetHello().GetContainers()))

	errs := make(chan error, 2)
	var wg sync.WaitGroup
	wg.Go(func() {
		for {
			command, err := stream.Recv()
			if err != nil {
				errs <- fmt.Errorf("receive command: %w", err)
				return
			}
			a.handle(command)
			ack := &hostproto.HostMessage{Body: &hostproto.HostMessage_Ack{Ack: &hostproto.Ack{CommandId: command.GetCommandId()}}}
			select {
			case out.ch <- ack:
			default:
				errs <- errors.New("session queue is full")
				return
			}
		}
	})
	wg.Go(func() {
		for {
			var m *hostproto.HostMessage
			select {
			case <-ctx.Done():
				errs <- ctx.Err()
				return
			case m = <-out.ch:
			}
			if err := stream.Send(m); err != nil {
				errs <- fmt.Errorf("send: %w", err)
				return
			}
			if r := m.GetContainer(); r.GetPhase() == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED {
				if c := a.lookup(r.GetContainerId()); c != nil {
					a.goOwned(c.cleanup)
				}
			}
		}
	})
	err = <-errs
	cancel()
	wg.Wait()
	return err
}

// hello describes every container, including exited ones within their
// retention, and returns the exited ones.
func (a *Agent) hello() (*hostproto.HostMessage, []*container) {
	a.mu.Lock()
	defer a.mu.Unlock()
	reports := make([]*hostproto.ContainerReport, 0, len(a.containers))
	var exited []*container
	for _, c := range a.containers {
		report := c.snapshot()
		reports = append(reports, report)
		if report.GetPhase() == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED {
			exited = append(exited, c)
		}
	}
	return &hostproto.HostMessage{Body: &hostproto.HostMessage_Hello{Hello: &hostproto.Hello{
		BootId:       a.bootID,
		Capacity:     a.capacity,
		Containers:   reports,
		AgentVersion: a.cfg.Version,
	}}}, exited
}

// report queues a message on the open session. Without one it is dropped;
// the next Hello carries the current state.
func (a *Agent) report(m *hostproto.HostMessage) {
	a.mu.Lock()
	out := a.session
	a.mu.Unlock()
	if out == nil {
		return
	}
	select {
	case out.ch <- m:
	default:
		a.log.Warn("session queue is full; reconnecting")
		out.cancel()
	}
}

// reportMetrics queues a metrics message only while the session queue is
// at most half full, so samples never crowd out reports and acks.
func (a *Agent) reportMetrics(m *hostproto.HostMessage) {
	a.mu.Lock()
	out := a.session
	a.mu.Unlock()
	if out == nil || len(out.ch) > cap(out.ch)/2 {
		return
	}
	select {
	case out.ch <- m:
	default:
	}
}

// handle dispatches a command without waiting on containers, so commands
// are never delayed by container work.
func (a *Agent) handle(command *hostproto.ServerMessage) {
	switch body := command.GetBody().(type) {
	case *hostproto.ServerMessage_Start:
		a.start(body.Start)
	case *hostproto.ServerMessage_Stop:
		a.stop(body.Stop)
	case *hostproto.ServerMessage_Cancel:
		if c := a.lookup(body.Cancel.GetContainerId()); c != nil {
			c.cancelAttempt(body.Cancel.GetAttemptId())
		}
	case *hostproto.ServerMessage_StorageGrant:
		if err := a.volumes.grant(body.StorageGrant); err != nil {
			a.log.Error("storing a storage grant failed", "workspace_id", body.StorageGrant.GetWorkspaceId(), "error", err)
		}
	default:
		a.log.Warn("ignoring unknown command", "command_id", command.GetCommandId())
	}
}

// start is idempotent by container id: a known container restates its state.
func (a *Agent) start(spec *hostproto.StartContainer) {
	id := spec.GetContainerId()
	if _, err := uuid.Parse(id); err != nil {
		a.log.Warn("ignoring start with an invalid container id", "container_id", id)
		return
	}
	if spec.GetBuild() != nil {
		a.startBuild(spec)
		return
	}
	a.mu.Lock()
	c, known := a.containers[id]
	if !known {
		c = a.newContainer(id, spec.GetFunction().GetHandler(), int(spec.GetFunction().GetSlots()), hostproto.ContainerPhase_CONTAINER_PHASE_PREPARING)
		c.runtime = runtimeOf(spec)
		a.containers[id] = c
	}
	a.mu.Unlock()
	c.report()
	if !known {
		a.goOwned(func(ctx context.Context) { c.launch(ctx, spec) })
	}
}

// stop drains a known container. An unknown one is reported stopped, since
// nothing of it runs here.
func (a *Agent) stop(stop *hostproto.StopContainer) {
	id := stop.GetContainerId()
	if _, err := uuid.Parse(id); err != nil {
		a.log.Warn("ignoring stop with an invalid container id", "container_id", id)
		return
	}
	a.mu.Lock()
	c, known := a.containers[id]
	if !known {
		c = a.newContainer(id, "", 0, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
		c.exit = &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_STOPPED, Message: "not running on this host"}
		c.exitedAt = time.Now()
		a.containers[id] = c
	}
	a.mu.Unlock()
	if !known {
		c.report()
		return
	}
	c.stop(time.Duration(stop.GetGraceSeconds()) * time.Second)
}
