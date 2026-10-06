package agent

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"sync"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

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

// sessions keeps a session open, reconnecting with backoff, until ctx ends
// or the server refuses the host's credential.
func (a *Agent) sessions(ctx context.Context) error {
	delay := minSessionBackoff
	for ctx.Err() == nil {
		began := time.Now()
		err := a.runSession(ctx)
		if ctx.Err() != nil {
			return nil //nolint:nilerr // the session ended because the agent is stopping
		}
		if status.Code(err) == codes.Unauthenticated {
			a.log.Error("host credential revoked; the machine was removed or its token replaced", "error", err)
			return errors.Join(ErrCredentialRevoked, forgetIdentity(a.cfg.StateDir))
		}
		if time.Since(began) > time.Minute {
			delay = minSessionBackoff
		}
		a.log.Warn("session ended", "error", err, "retry_in", delay)
		backoff := time.NewTimer(delay/2 + rand.N(delay/2+1)) //nolint:gosec // jitter needs no cryptographic randomness
		select {
		case <-ctx.Done():
			backoff.Stop()
			return nil
		case <-a.reconnectNow:
			// A resume: the server has been away, not refusing.
			backoff.Stop()
			delay = minSessionBackoff
			continue
		case <-backoff.C:
		}
		delay = min(2*delay, maxSessionBackoff)
	}
	return nil
}

// runSession opens the stream with a Hello describing every container, then
// handles commands and sends reports until the stream fails.
func (a *Agent) runSession(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	// A Hello that cannot list them names none: the session matters more
	// than renewing grants a running container's copy has for an hour.
	running, err := a.runningPlatformImages(ctx)
	if err != nil {
		if ctx.Err() != nil {
			return err
		}
		a.log.Warn("listing the platform images containers run failed; the Hello names none", "error", err)
	}
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
	// A failure the last session sent may be stale; waiters take this
	// session's answer.
	a.platform.forgetFailures()
	hello, exited := a.hello(running)
	if err := stream.Send(hello); err != nil {
		// A stream the server refused ends sends with io.EOF; its status
		// comes with the next receive.
		if errors.Is(err, io.EOF) {
			_, err = stream.Recv()
		}
		return fmt.Errorf("send hello: %w", err)
	}
	a.mu.Lock()
	notice, trial := a.interruption, a.trial
	a.mu.Unlock()
	if notice != nil {
		if err := stream.Send(notice.message()); err != nil {
			return fmt.Errorf("send interruption: %w", err)
		}
	}
	for _, c := range exited {
		a.goOwned(c.cleanup)
	}
	a.log.Info("session open", "containers", len(hello.GetHello().GetContainers()))

	errs := make(chan error, 2)
	var wg sync.WaitGroup
	if trial != "" {
		wg.Go(func() { a.commitAfterSession(ctx) })
	}
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
// retention, and the platform image copies running ones run, and returns
// the exited ones.
func (a *Agent) hello(running []string) (*hostproto.HostMessage, []*container) {
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
		BootId:                a.bootID,
		Capacity:              a.capacity,
		Containers:            reports,
		AgentVersion:          a.cfg.Version,
		Updatable:             a.updatable,
		RejectedVersion:       readMarker(a.cfg.StateDir, RejectedFile),
		SleptSeconds:          sleptSince(a.sleep, a.bootID, a.clock.gap()).Seconds(),
		SleepAttemptId:        a.sleep.ID,
		PlatformImages:        a.platform.named,
		RunningPlatformImages: running,
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
	case *hostproto.ServerMessage_Update:
		a.update(body.Update)
	case *hostproto.ServerMessage_PrepareReserve:
		a.prepareReserve(body.PrepareReserve)
	case *hostproto.ServerMessage_Network:
		if c := a.lookup(body.Network.GetContainerId()); c != nil {
			c.updateNetwork(body.Network.GetPolicy(), body.Network.GetVersion())
		}
	case *hostproto.ServerMessage_Snapshot:
		a.snapshot(body.Snapshot)
	case *hostproto.ServerMessage_PublishFilesystem:
		a.publishFilesystem(body.PublishFilesystem)
	case *hostproto.ServerMessage_LayerGrants:
		a.layers.refresh(body.LayerGrants.GetLayers())
	case *hostproto.ServerMessage_PlatformImages:
		// Mount and builder containers keep reading their images' layers.
		for _, image := range body.PlatformImages.GetImages() {
			a.layers.refresh(image.GetLayers())
		}
		a.platform.update(body.PlatformImages.GetImages())
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
		c = a.newContainer(id, spec.GetFunction().GetHandler(), int(spec.GetFunction().GetSlots()), spec.GetHttp(), hostproto.ContainerPhase_CONTAINER_PHASE_PREPARING)
		c.runtime = runtimeOf(spec)
		c.docker = spec.GetDocker()
		c.checkpointable = spec.GetCheckpointable()
		a.containers[id] = c
	}
	a.mu.Unlock()
	c.report()
	if !known {
		a.goOwned(func(ctx context.Context) { c.launch(ctx, spec) })
		return
	}
	// A start sent again, as after a reconnect, carries fresh grants.
	a.layers.refresh(spec.GetLayers())
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
		c = a.newContainer(id, "", 0, nil, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
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
