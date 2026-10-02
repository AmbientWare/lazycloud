// Package supervisor is PID 1 in every workload container. It runs one runner
// process per slot over the local runner protocol, or a pod's command,
// captures output, kills a slot's process group on cancellation and reports
// to the agent over the container's ContainerLink socket. It serves the
// control API: processes, files, shells, SSH and port tunnels.
package supervisor

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"slices"
	"sync"
	"sync/atomic"
	"time"

	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc"
	"google.golang.org/grpc/backoff"
	"google.golang.org/grpc/credentials/insecure"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// SocketEnv names the agent's ContainerLink socket inside the container.
const SocketEnv = "LAZYCLOUD_SUPERVISOR_SOCKET"

// MaxMessageBytes bounds link messages; task inputs and results are at most
// 16 MiB each.
const MaxMessageBytes = 64 << 20

const (
	minBackoff = 50 * time.Millisecond
	maxBackoff = 2 * time.Second
	// flushTimeout bounds delivery of the final messages after a drain or a
	// load failure.
	flushTimeout = 30 * time.Second
)

// ErrLoadFailed reports that a runner could not load the handler. The
// supervisor sent LoadFailed before returning it.
var ErrLoadFailed = errors.New("a runner failed to load the handler")

// Config configures a supervisor.
type Config struct {
	// Socket is the agent's ContainerLink socket.
	Socket string
	// APISocket is where the container API is served; empty serves none.
	APISocket string
	// Reap waits for orphaned processes, which PID 1 must do.
	Reap   bool
	Logger *slog.Logger
}

// Supervisor owns the runner slots of one container.
type Supervisor struct {
	cfg      Config
	log      *slog.Logger
	out      *outbox
	children *children

	mu        sync.Mutex
	configure *hostproto.Configure
	// redact is set by Configure, before any slot runs.
	redact *redactor
	slots  []*slot
	loaded map[*slot]bool
	runs   chan *hostproto.RunAttempt
	// queued maps attempts waiting for a slot to whether they were cancelled.
	queued map[string]bool
	ready  bool
	// readySlots is the slot count reported ready: every runner slot, or a
	// pod's one.
	readySlots     int32
	loadFailedSent bool
	draining       bool

	// workers are the HTTP workers of an HTTP workload.
	workers httpWorkers

	// sockets are the ones served on the host's side of the container;
	// detached says a Detach closed them until the next connection.
	sockets  []hostSocket
	detached atomic.Bool

	configured chan struct{}
	drain      chan struct{}
	// finishing is closed once nothing more will be pushed to out.
	finishing  chan struct{}
	finishOnce sync.Once
	flushed    atomic.Bool
}

// Run supervises until the agent drains the container (nil), a handler fails
// to load (ErrLoadFailed) or ctx ends. Cancelling ctx terminates every runner.
func Run(ctx context.Context, cfg Config) error {
	s := &Supervisor{
		cfg:        cfg,
		log:        cfg.Logger,
		out:        newOutbox(),
		children:   newChildren(),
		loaded:     make(map[*slot]bool),
		queued:     make(map[string]bool),
		configured: make(chan struct{}),
		drain:      make(chan struct{}),
		finishing:  make(chan struct{}),
	}
	conn, err := grpc.NewClient("unix:"+cfg.Socket,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		// The link comes back within seconds of an agent restart or a
		// checkpoint.
		grpc.WithConnectParams(grpc.ConnectParams{
			Backoff:           backoff.Config{BaseDelay: minBackoff, Multiplier: 1.6, Jitter: 0.2, MaxDelay: maxBackoff},
			MinConnectTimeout: time.Second,
		}),
		grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(MaxMessageBytes), grpc.MaxCallSendMsgSize(MaxMessageBytes)),
	)
	if err != nil {
		return fmt.Errorf("create agent link client: %w", err)
	}
	defer func() { _ = conn.Close() }()

	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	var wg sync.WaitGroup
	if cfg.Reap {
		wg.Go(func() { s.children.reapOrphans(runCtx) })
	}
	client := hostproto.NewContainerLinkClient(conn)
	if cfg.APISocket != "" {
		wg.Go(func() {
			if err := s.serveAPI(runCtx, cfg.APISocket, client); err != nil {
				s.log.Error("container API stopped", "error", err)
			}
		})
	}
	var result error
	wg.Go(func() {
		result = s.work(runCtx)
		s.finishOnce.Do(func() { close(s.finishing) })
		select {
		case <-time.After(flushTimeout):
			s.log.Error("gave up delivering final messages to the agent")
			cancel()
		case <-runCtx.Done():
		}
	})
	linkErr := s.link(runCtx, client)
	cancel()
	wg.Wait()
	if result == nil && linkErr != nil && ctx.Err() == nil && !s.flushed.Load() {
		return linkErr
	}
	return result
}

// work runs the slots once the agent configures them.
func (s *Supervisor) work(ctx context.Context) error {
	select {
	case <-ctx.Done():
		return fmt.Errorf("wait for configure: %w", ctx.Err())
	case <-s.drain:
		return nil
	case <-s.configured:
	}
	s.mu.Lock()
	cfg, slots, runs := s.configure, s.slots, s.runs
	s.mu.Unlock()
	if root := cfg.GetPod().GetRoot(); root != "" {
		if err := enterRoot(root); err != nil {
			s.loadFailed(&hostproto.RunnerError{Type: "DevboxRootError", Message: err.Error()})
			return ErrLoadFailed
		}
		// A Docker daemon keeps the privileged container's capabilities.
		if !cfg.GetDocker() {
			if err := dropMountPrivilege(); err != nil {
				s.loadFailed(&hostproto.RunnerError{Type: "DevboxRootError", Message: err.Error()})
				return ErrLoadFailed
			}
		}
	}
	if path := cfg.GetControlSocket(); path != "" {
		ctl, err := newControl(s.log, s.children, cfg.GetPod().GetSsh())
		if err == nil {
			err = ctl.listen(ctx, path)
		}
		if err != nil {
			s.loadFailed(&hostproto.RunnerError{Type: "SupervisorStartError", Message: err.Error()})
			return ErrLoadFailed
		}
		defer ctl.close()
		s.holdSocket(ctl)
		defer s.dropSocket(ctl)
	}
	if cfg.GetDocker() {
		docker, err := s.startDocker(ctx)
		if err != nil {
			s.loadFailed(&hostproto.RunnerError{Type: "DockerStartError", Message: err.Error()})
			return ErrLoadFailed
		}
		defer docker.stop()
	}
	if cfg.GetPod() != nil {
		return s.runPod(ctx, cfg.GetPod())
	}
	s.log.Info("starting runners", "slots", len(slots), "handler", cfg.GetHandler(), "http", cfg.GetHttp() != nil)
	var front *httpFront
	if cfg.GetHttp() != nil {
		var err error
		if front, err = s.listenHTTP(ctx, cfg); err != nil {
			s.loadFailed(&hostproto.RunnerError{Type: "SupervisorStartError", Message: err.Error()})
			return ErrLoadFailed
		}
	}
	var serveErr error
	if front != nil {
		s.holdSocket(front)
	}
	g, gctx := errgroup.WithContext(ctx)
	// HTTP workers admit concurrent requests themselves.
	if cfg.GetInProcess() && len(slots) > 1 && front == nil {
		g.Go(func() error { return s.runShared(gctx, cfg, slots, runs) })
	} else {
		for _, sl := range slots {
			g.Go(func() error { return sl.run(gctx, cfg, runs) })
		}
	}
	err := g.Wait()
	if front != nil {
		// Every slot has finished its requests.
		s.dropSocket(front)
		serveErr = front.close()
	}
	if err != nil {
		return fmt.Errorf("run slots: %w", err)
	}
	return serveErr
}

// link keeps a ContainerLink stream open, reconnecting after the agent
// restarts, until the final messages are delivered or ctx ends.
func (s *Supervisor) link(ctx context.Context, client hostproto.ContainerLinkClient) error {
	delay := minBackoff
	for {
		connected, err := s.connect(ctx, client)
		if s.flushed.Load() {
			return nil
		}
		if ctx.Err() != nil {
			return fmt.Errorf("agent link: %w", ctx.Err())
		}
		if connected {
			delay = minBackoff
		}
		s.log.Warn("agent link lost", "error", err)
		select {
		case <-ctx.Done():
			return fmt.Errorf("agent link: %w", ctx.Err())
		case <-time.After(delay):
		}
		delay = min(delay*2, maxBackoff)
	}
}

func (s *Supervisor) connect(ctx context.Context, client hostproto.ContainerLinkClient) (bool, error) {
	streamCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := client.Connect(streamCtx, grpc.WaitForReady(true))
	if err != nil {
		return false, fmt.Errorf("connect to agent: %w", err)
	}
	s.reattach(ctx)
	if ready := s.readyMessage(); ready != nil {
		if err := stream.Send(ready); err != nil {
			return true, fmt.Errorf("send slots ready: %w", err)
		}
	}
	recvDone := make(chan struct{})
	g, gctx := errgroup.WithContext(streamCtx)
	g.Go(func() error {
		defer close(recvDone)
		for {
			command, err := stream.Recv()
			if errors.Is(err, io.EOF) {
				return errors.New("agent closed the link")
			}
			if err != nil {
				return fmt.Errorf("receive command: %w", err)
			}
			s.handle(command)
		}
	})
	g.Go(func() error { return s.send(gctx, stream, recvDone) })
	if err := g.Wait(); err != nil {
		return true, fmt.Errorf("link stream: %w", err)
	}
	return true, nil
}

// send delivers the outbox in order. After the last message it half-closes
// the stream and waits for the agent to finish reading it.
func (s *Supervisor) send(ctx context.Context, stream hostproto.ContainerLink_ConnectClient, recvDone <-chan struct{}) error {
	for {
		m := s.out.front()
		if m != nil {
			if err := stream.Send(m); err != nil {
				return fmt.Errorf("send to agent: %w", err)
			}
			s.out.pop()
			continue
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("send to agent: %w", ctx.Err())
		case <-s.out.wake:
		case <-s.finishing:
			if !s.out.empty() {
				continue
			}
			if err := stream.CloseSend(); err != nil {
				return fmt.Errorf("close link: %w", err)
			}
			s.flushed.Store(true)
			select {
			case <-recvDone:
			case <-ctx.Done():
			}
			return nil
		}
	}
}

func (s *Supervisor) handle(command *hostproto.SupervisorCommand) {
	switch body := command.GetBody().(type) {
	case *hostproto.SupervisorCommand_Configure:
		s.onConfigure(body.Configure)
	case *hostproto.SupervisorCommand_Run:
		s.onRun(body.Run)
	case *hostproto.SupervisorCommand_Cancel:
		s.cancel(body.Cancel.GetAttemptId())
	case *hostproto.SupervisorCommand_Drain:
		s.onDrain()
	case *hostproto.SupervisorCommand_Reload:
		s.onReload()
	case *hostproto.SupervisorCommand_Detach:
		s.onDetach()
	default:
		s.log.Warn("ignoring unknown supervisor command")
	}
}

// hostSocket is a socket served on the host's side of the container,
// which no checkpoint can hold.
type hostSocket interface {
	release()
	reopen(ctx context.Context) error
}

func (s *Supervisor) holdSocket(h hostSocket) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sockets = append(s.sockets, h)
}

func (s *Supervisor) dropSocket(h hostSocket) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.sockets = slices.DeleteFunc(s.sockets, func(held hostSocket) bool { return held == h })
}

// onDetach closes the host sockets and their connections for a checkpoint.
// The agent closes the link once Detached arrives, and the next connection
// opens the sockets again.
func (s *Supervisor) onDetach() {
	s.mu.Lock()
	sockets := slices.Clone(s.sockets)
	s.mu.Unlock()
	s.detached.Store(true)
	for _, h := range sockets {
		h.release()
	}
	s.log.Info("detached for a checkpoint")
	s.out.push(&hostproto.SupervisorMessage{Body: &hostproto.SupervisorMessage_Detached{Detached: &hostproto.Detached{}}})
}

// reattach opens again the sockets a Detach closed.
func (s *Supervisor) reattach(ctx context.Context) {
	if !s.detached.Swap(false) {
		return
	}
	s.mu.Lock()
	sockets := slices.Clone(s.sockets)
	s.mu.Unlock()
	for _, h := range sockets {
		if err := h.reopen(ctx); err != nil {
			s.log.Error("reopening a socket after a checkpoint failed", "error", err)
		}
	}
	s.log.Info("reattached after a checkpoint")
}

// onConfigure creates the slots. The agent sends Configure on every
// connection, so a repeat is ignored.
func (s *Supervisor) onConfigure(c *hostproto.Configure) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.configure != nil || s.draining {
		return
	}
	s.configure = c
	s.redact = newRedactor(c.GetSecretEnv())
	if c.GetPod() != nil {
		// A pod runs its command instead of runner slots.
		close(s.configured)
		return
	}
	n := max(1, int(c.GetSlots()))
	s.runs = make(chan *hostproto.RunAttempt, n)
	for range n {
		sl := &slot{sup: s, buf: make([]byte, readChunk), reload: make(chan struct{}, 1)}
		if c.GetHttp() != nil {
			sl.http = s.workers.add()
		}
		s.slots = append(s.slots, sl)
	}
	close(s.configured)
}

// onReload restarts every runner once its current work finishes, so source
// synced into the workspace takes effect.
func (s *Supervisor) onReload() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, sl := range s.slots {
		select {
		case sl.reload <- struct{}{}:
		default:
		}
	}
}

func (s *Supervisor) onRun(run *hostproto.RunAttempt) {
	s.mu.Lock()
	defer s.mu.Unlock()
	id := run.GetAttemptId()
	if s.configure == nil || s.draining || s.configure.GetHttp() != nil || s.configure.GetPod() != nil {
		s.out.push(finishedMessage(crashed(id, "NotAccepting", "the supervisor is not accepting attempts")))
		return
	}
	if _, queued := s.queued[id]; queued || s.runningLocked(id) != nil {
		return
	}
	s.queued[id] = false
	select {
	case s.runs <- run:
	default:
		delete(s.queued, id)
		s.out.push(finishedMessage(crashed(id, "NoIdleSlot", "every slot is busy")))
	}
}

func (s *Supervisor) onDrain() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.draining {
		return
	}
	s.draining = true
	close(s.drain)
	if s.runs != nil {
		close(s.runs)
	}
}

// begin assigns a dequeued attempt to a slot unless it was cancelled while
// queued. It holds s.mu so a concurrent cancel finds the attempt either
// queued or running.
func (s *Supervisor) begin(sl *slot, attempt string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	cancelled := s.queued[attempt]
	delete(s.queued, attempt)
	if cancelled {
		return false
	}
	sl.outMu.Lock()
	sl.drainLocked(true)
	sl.attempt, sl.cancelled = attempt, false
	sl.outMu.Unlock()
	return true
}

// cancel kills the slot running attempt, or drops it if it is still queued.
func (s *Supervisor) cancel(attempt string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, queued := s.queued[attempt]; queued {
		s.queued[attempt] = true
		return
	}
	if sl := s.runningLocked(attempt); sl != nil {
		sl.outMu.Lock()
		sl.cancelled = true
		p := sl.proc
		sl.outMu.Unlock()
		if p != nil {
			p.terminate()
		}
	}
}

func (s *Supervisor) runningLocked(attempt string) *slot {
	for _, sl := range s.slots {
		sl.outMu.Lock()
		running := sl.attempt == attempt
		sl.outMu.Unlock()
		if running {
			return sl
		}
	}
	return nil
}

func (s *Supervisor) slotLoaded(sl *slot) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.loaded[sl] = true
	if !s.ready && len(s.loaded) == len(s.slots) {
		s.ready, s.readySlots = true, int32(len(s.slots)) //nolint:gosec // slots come from an int32
		s.out.push(&hostproto.SupervisorMessage{Body: &hostproto.SupervisorMessage_Ready{
			Ready: &hostproto.SlotsReady{Slots: int32(len(s.slots))}, //nolint:gosec // slots come from an int32
		}})
	}
}

// readyMessage restates readiness and occupied slots for a new connection.
func (s *Supervisor) readyMessage() *hostproto.SupervisorMessage {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.ready {
		return nil
	}
	var running []string
	for attempt, cancelled := range s.queued {
		if !cancelled {
			running = append(running, attempt)
		}
	}
	for _, sl := range s.slots {
		sl.outMu.Lock()
		if sl.attempt != "" && !sl.cancelled {
			running = append(running, sl.attempt)
		}
		sl.outMu.Unlock()
	}
	// A slot pushes its outcome as it clears its attempt, so reading slots
	// first finds every attempt in one place or the other. The agent accepts
	// outcomes only for attempts it counts as running.
	running = append(running, s.out.unreportedAttempts()...)
	return &hostproto.SupervisorMessage{Body: &hostproto.SupervisorMessage_Ready{Ready: &hostproto.SlotsReady{
		Slots:           s.readySlots,
		RunningAttempts: running,
	}}}
}

func (s *Supervisor) loadFailed(e *hostproto.RunnerError) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.loadFailedSent {
		return
	}
	s.loadFailedSent = true
	e.Message, e.Traceback = s.redact.all(e.GetMessage()), s.redact.all(e.GetTraceback())
	s.log.Error("handler failed to load", "type", e.GetType(), "message", e.GetMessage())
	s.out.push(&hostproto.SupervisorMessage{Body: &hostproto.SupervisorMessage_LoadFailed{
		LoadFailed: &hostproto.LoadFailed{Error: e},
	}})
}

func (s *Supervisor) isDraining() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.draining
}

func finishedMessage(f *hostproto.AttemptFinished) *hostproto.SupervisorMessage {
	return &hostproto.SupervisorMessage{Body: &hostproto.SupervisorMessage_Finished{Finished: f}}
}
