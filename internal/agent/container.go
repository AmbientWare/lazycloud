package agent

import (
	"context"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	claimWaitSeconds = 20
	// completeCallTimeout bounds one CompleteTask call. The call in flight
	// when the agent stops still finishes.
	completeCallTimeout = 15 * time.Second
	maxCompleteBackoff  = 5 * time.Second
	// cancelledRetention and maxCancelled bound the cancels remembered for
	// attempts that may still arrive in a claim response.
	cancelledRetention = 10 * time.Minute
	maxCancelled       = 4096
	// exitSettle bounds how long an exit waits for the link, completions and
	// logs before it is reported.
	exitSettle = 10 * time.Second
	// stopKillSeconds is docker stop's SIGTERM-to-SIGKILL delay once a drain
	// has run out of grace.
	stopKillSeconds = 5
)

// container is one workload container on this host. Its state is what the
// agent observed; the server's durable state decides what should happen.
type container struct {
	a       *Agent
	log     *slog.Logger
	id      string
	dir     string
	handler string
	slots   int
	// runtime is how the supervisor runs the slots: in one process, with
	// which hooks, and which environment variables hold secrets.
	runtime containerRuntime
	logs    *logBatcher
	// http is set for HTTP workloads, whose slots serve requests forwarded
	// over the data connection instead of claiming tasks.
	http *hostproto.HttpServing
	// requests reaches the supervisor's HTTP socket.
	requests *http.Transport
	// pod is set for containers that run a command instead of runner
	// slots; docker runs a Docker daemon in the container.
	pod    *hostproto.PodProcess
	docker bool
	// control reaches the supervisor's control API, and ports the
	// container's ports through it.
	control *http.Transport
	ports   *http.Transport
	network networkState
	disks   diskSet
	// apiCalls bounds container API calls in flight.
	apiCalls chan struct{}

	// work covers preparation, claims and log delivery; claims stop earlier
	// when the container begins stopping.
	work         context.Context
	cancelWork   context.CancelFunc
	claims       context.Context
	cancelClaims context.CancelFunc
	slotFree     chan struct{}
	// gone is closed once the Docker container is observed to have exited.
	gone        chan struct{}
	completions sync.WaitGroup

	mu       sync.Mutex
	phase    hostproto.ContainerPhase
	exit     *hostproto.ContainerExit
	exitedAt time.Time
	cleaned  bool
	started  bool
	running  map[string]struct{}
	// completing holds running attempts whose outcome is being delivered.
	completing map[string]struct{}
	cancelled  cancelledAttempts
	stopping   bool
	grace      time.Duration
	loadError  *hostproto.RunnerError
	// commandExit is a pod command's exit code once it ended on its own.
	commandExit *int32
	// restoreFailed is the snapshot the start could not restore.
	restoreFailed string
	// volumeLost says why the container was stopped for a dead mount.
	volumeLost string
	link       *link
	claiming   bool
	// build marks an image build container, which has no link or slots.
	isBuild bool

	// gpus are the UUIDs of the devices the container holds, guarded by the
	// agent's mutex.
	gpus []string
	// startup holds the finished start stages; created is when the
	// container process started, which begins the runtime stage.
	startup []*hostproto.StartupStage
	created time.Time
	// traces holds, per running attempt, its start and the trace of the
	// request that submitted it.
	traces map[string]attemptTrace

	// usage is where the sampler reads the container's use, once known.
	usage atomic.Pointer[usageSource]
}

type attemptTrace struct {
	task        string
	traceparent string
	started     time.Time
}

func (a *Agent) newContainer(id, handler string, slots int, httpServing *hostproto.HttpServing, phase hostproto.ContainerPhase) *container {
	c := &container{
		a:          a,
		log:        a.log.With("container_id", id),
		id:         id,
		dir:        filepath.Join(a.cfg.StateDir, "containers", id),
		handler:    handler,
		slots:      max(1, slots),
		http:       httpServing,
		phase:      phase,
		running:    make(map[string]struct{}),
		completing: make(map[string]struct{}),
		traces:     make(map[string]attemptTrace),
		cancelled:  cancelledAttempts{at: make(map[string]time.Time)},
		slotFree:   make(chan struct{}, 1),
		gone:       make(chan struct{}),
		apiCalls:   make(chan struct{}, maxContainerAPICalls),
	}
	if httpServing != nil {
		c.requests = socketTransport(filepath.Join(c.linkDir(), httpSocketName))
	}
	control := filepath.Join(c.linkDir(), controlSocketName)
	c.control = socketTransport(control)
	c.ports = portTransport(control)
	c.work, c.cancelWork = context.WithCancel(a.ctx)
	c.claims, c.cancelClaims = context.WithCancel(c.work)
	c.logs = newLogBatcher(id, a.host, c.log)
	if phase == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED {
		c.cancelWork()
		close(c.gone)
	} else {
		a.goOwned(func(context.Context) { c.logs.run(c.work) }) //nolint:contextcheck // logs live as long as the container's work
	}
	return c
}

func (c *container) workspaceDir() string { return filepath.Join(c.dir, "workspace") }
func (c *container) linkDir() string      { return filepath.Join(c.a.cfg.SocketDir, c.id) }
func (c *container) dockerName() string   { return "lazycloud-" + c.id }

func (c *container) configure() *hostproto.Configure {
	configure := &hostproto.Configure{
		SecretEnv:     c.runtime.SecretEnv,
		ControlSocket: containerControlSocket,
		Docker:        c.docker,
	}
	if c.pod != nil {
		configure.Pod = c.pod
		configure.WorkingDirectory = c.pod.GetWorkingDirectory()
		return configure
	}
	configure.Handler = c.handler
	configure.Slots = int32(c.slots) //nolint:gosec // slots come from an int32
	configure.RunnerCommand = []string{"python3", "-m", "runner"}
	configure.WorkingDirectory = containerWorkspace
	configure.InProcess = c.runtime.InProcess
	configure.Hooks = c.runtime.Hooks
	if c.http != nil {
		configure.Http = c.http
		configure.HttpSocket = containerLinkDir + "/" + httpSocketName
	}
	return configure
}

// snapshot is the container's current report.
func (c *container) snapshot() *hostproto.ContainerReport {
	c.mu.Lock()
	defer c.mu.Unlock()
	running := make([]string, 0, len(c.running))
	for attempt := range c.running {
		running = append(running, attempt)
	}
	slices.Sort(running)
	return &hostproto.ContainerReport{
		ContainerId:     c.id,
		Phase:           c.phase,
		Exit:            c.exit,
		ObservedAt:      timestamppb.Now(),
		RunningAttempts: running,
		Startup:         slices.Clone(c.startup),
		RestoreFailed:   c.restoreFailed,
	}
}

// addStage records a finished start stage and reports it, so the server
// sees each stage of a slow start as it ends.
func (c *container) addStage(kind hostproto.StartupStageKind, started, finished time.Time, cached bool) {
	c.mu.Lock()
	c.startup = append(c.startup, &hostproto.StartupStage{Kind: kind,
		StartedAt: timestamppb.New(started), FinishedAt: timestamppb.New(finished), Cached: cached})
	c.mu.Unlock()
	c.report()
}

// reachable reports whether the container's supervisor may serve its
// control API: the container started here and has not exited.
func (c *container) reachable() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.started && c.phase != hostproto.ContainerPhase_CONTAINER_PHASE_EXITED
}

func (c *container) report() {
	c.a.report(&hostproto.HostMessage{Body: &hostproto.HostMessage_Container{Container: c.snapshot()}})
}

// launch prepares and starts the container, then watches it until it exits.
func (c *container) launch(ctx context.Context, spec *hostproto.StartContainer) {
	if err := c.prepare(c.work, spec); err != nil { //nolint:contextcheck // stopping cancels preparation, not the watch
		reason := hostproto.ExitReason_EXIT_REASON_START_FAILED
		if c.isStopping() {
			reason = hostproto.ExitReason_EXIT_REASON_STOPPED
		}
		c.log.Warn("container did not start", "error", err, "reason", reason)
		c.exited(&hostproto.ContainerExit{Reason: reason, Message: err.Error()})
		return
	}
	c.mu.Lock()
	c.started = true
	if c.phase == hostproto.ContainerPhase_CONTAINER_PHASE_PREPARING {
		c.phase = hostproto.ContainerPhase_CONTAINER_PHASE_STARTING
	}
	stopping := c.stopping
	l := c.link
	c.mu.Unlock()
	c.report()
	l.serve()
	if stopping {
		c.a.goOwned(c.stopRunning)
	}
	c.watch(ctx)
}

// prepare fetches the image and source, attaches disks, creates the link
// socket, starts the Docker container and applies its network policy. Each
// stage is timed and reported as it finishes.
func (c *container) prepare(ctx context.Context, spec *hostproto.StartContainer) error {
	pod := spec.GetPod()
	if pod == nil && spec.GetFunction().GetHandler() == "" {
		return fmt.Errorf("start has neither a function handler nor a pod")
	}
	// Functions run the managed Python runner; a pod mounts the runtime
	// only when the start names one.
	var runtime string
	if version := spec.GetPythonVersion(); pod == nil || version != "" {
		var err error
		if runtime, err = c.a.pythonRuntime(version); err != nil {
			return err
		}
	}
	var gpus []string
	if n := int(spec.GetResources().GetGpuCount()); n > 0 {
		var err error
		if gpus, err = c.a.allocateGPUs(c, n); err != nil {
			return err
		}
	}

	began := time.Now()
	pulled, err := c.a.images.ensure(ctx, spec.GetImage(), spec.GetImageAuth(), spec.GetImagePlatform())
	if err != nil {
		return err
	}
	if pod != nil {
		process, err := c.a.podProcess(ctx, spec)
		if err != nil {
			return err
		}
		c.mu.Lock()
		c.pod = process
		c.mu.Unlock()
	}
	imageReady := time.Now()
	c.addStage(hostproto.StartupStageKind_STARTUP_STAGE_KIND_IMAGE, began, imageReady, !pulled)

	// A devbox's root disk is its filesystem; it gets no source.
	if !pod.GetDevbox() {
		if err := c.prepareWorkspace(ctx, spec.GetSource()); err != nil {
			return err
		}
	}
	stageEnd := time.Now()
	c.addStage(hostproto.StartupStageKind_STARTUP_STAGE_KIND_SOURCE, imageReady, stageEnd, false)

	if pod.GetDevbox() && !slices.ContainsFunc(spec.GetDisks(), func(d *hostproto.DiskAttachment) bool { return d.GetMountPath() == "/" }) {
		return fmt.Errorf("a devbox needs a disk mounted at /")
	}
	var diskBinds []mount.Mount
	var workspaces []string
	if len(spec.GetDisks()) > 0 {
		diskStart := stageEnd
		if diskBinds, workspaces, err = c.attachDisks(ctx, spec.GetDisks(), pod.GetDevbox()); err != nil {
			return err
		}
		stageEnd = time.Now()
		c.addStage(hostproto.StartupStageKind_STARTUP_STAGE_KIND_DISK, diskStart, stageEnd, false)
	}

	l, err := listenLink(ctx, c, c.linkDir())
	if err != nil {
		return err
	}
	c.mu.Lock()
	c.link = l
	c.mu.Unlock()
	binds, volumeWorkspaces, err := c.a.volumes.binds(ctx, c.id, spec.GetVolumes())
	if err != nil {
		return err
	}
	binds = append(binds, diskBinds...)
	for _, ws := range volumeWorkspaces {
		if !slices.Contains(workspaces, ws) {
			workspaces = append(workspaces, ws)
		}
	}
	restore := c.prepareRestore(ctx, spec.GetRestore())
	if err := c.a.createAndStart(ctx, c, spec, runtime, binds, workspaces, gpus, restore); err != nil {
		return err
	}
	// The supervisor waits for Configure, which the link serves only after
	// this, so the command never runs before its filter is in place.
	if err := c.applyStartNetwork(ctx, pod.GetNetwork()); err != nil {
		return err
	}
	created := time.Now()
	c.mu.Lock()
	c.created = created
	c.mu.Unlock()
	c.addStage(hostproto.StartupStageKind_STARTUP_STAGE_KIND_CREATE, stageEnd, created, false)
	c.watchUsage(ctx)
	c.log.Info("container started",
		"image_pulled", pulled,
		"image_ms", imageReady.Sub(began).Milliseconds(),
		"start_ms", created.Sub(imageReady).Milliseconds(),
	)
	return nil
}

// pythonRuntime is the host directory of an installed Python runtime.
func (a *Agent) pythonRuntime(version string) (string, error) {
	if version == "" || !filepath.IsLocal(version) || filepath.Base(version) != version {
		return "", fmt.Errorf("python version %q is not a runtime name", version)
	}
	runtime := filepath.Join(a.cfg.RuntimeDir, version)
	if info, err := os.Stat(runtime); err != nil || !info.IsDir() {
		return "", fmt.Errorf("python runtime %s is not installed at %s", version, runtime)
	}
	return runtime, nil
}

// prepareWorkspace extracts the source into the workspace, or leaves it
// empty for a pod without one.
func (c *container) prepareWorkspace(ctx context.Context, source *hostproto.Source) error {
	if source == nil && c.pod != nil {
		if err := os.MkdirAll(c.workspaceDir(), 0o755); err != nil { //nolint:gosec // the container reads its workspace
			return fmt.Errorf("create workspace: %w", err)
		}
		return nil
	}
	archive, err := c.a.sources.fetch(ctx, source)
	if err != nil {
		return err
	}
	return extractWorkspace(archive, c.workspaceDir())
}

// watch waits for the Docker container to exit. Agent shutdown leaves it
// running; the next agent adopts it.
func (c *container) watch(ctx context.Context) {
	for {
		exited, err := c.a.waitExit(ctx, c.dockerName())
		if exited {
			break
		}
		if ctx.Err() != nil {
			return
		}
		c.log.Warn("waiting for container exit failed", "error", err)
		if !sleep(ctx, time.Second) {
			return
		}
	}
	c.observeExit(ctx)
}

// observeExit settles an exit: remaining supervisor messages, completions and
// logs are delivered before the exit is reported, so the server never marks
// attempts lost whose outcomes the agent holds.
func (c *container) observeExit(ctx context.Context) {
	close(c.gone)
	state, inspectErr := c.a.exitState(ctx, c.dockerName())
	c.mu.Lock()
	l := c.link
	c.mu.Unlock()
	if l != nil {
		l.close(exitSettle)
	}
	settled := make(chan struct{})
	c.a.goOwned(func(context.Context) {
		c.completions.Wait()
		close(settled)
	})
	timer := time.NewTimer(exitSettle)
	select {
	case <-settled:
	case <-timer.C:
		c.log.Warn("reporting exit before every completion was delivered")
	}
	timer.Stop()
	flushCtx, cancel := context.WithTimeout(ctx, exitSettle)
	c.logs.waitFlushed(flushCtx, c.logs.mark())
	cancel()

	exit := &hostproto.ContainerExit{Reason: hostproto.ExitReason_EXIT_REASON_CRASHED}
	if inspectErr != nil {
		exit.Message = inspectErr.Error()
	} else {
		exit.ExitCode = int32(state.ExitCode) //nolint:gosec // exit codes fit
		exit.Message = fmt.Sprintf("container exited with code %d", state.ExitCode)
		if state.Error != "" {
			exit.Message += ": " + state.Error
		}
	}
	c.mu.Lock()
	switch {
	case c.stopping:
		exit.Reason = hostproto.ExitReason_EXIT_REASON_STOPPED
	case c.loadError != nil:
		exit.Reason = hostproto.ExitReason_EXIT_REASON_LOAD_ERROR
		exit.Error = c.loadError
		exit.Message = c.loadError.GetMessage()
	case inspectErr == nil && state.OOMKilled:
		exit.Reason = hostproto.ExitReason_EXIT_REASON_OUT_OF_MEMORY
	case c.commandExit != nil:
		exit.Reason = hostproto.ExitReason_EXIT_REASON_EXITED
		exit.ExitCode = *c.commandExit
		exit.Message = fmt.Sprintf("the command exited with code %d", *c.commandExit)
	case c.volumeLost != "":
		exit.Message = c.volumeLost
	}
	c.mu.Unlock()
	c.exited(exit)
}

// exited records the final state and reports it. Cleanup waits until the
// report reaches the server, so an agent restart before then still finds the
// Docker container and reports its exit.
func (c *container) exited(exit *hostproto.ContainerExit) {
	c.mu.Lock()
	if c.phase == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED {
		c.mu.Unlock()
		return
	}
	c.phase = hostproto.ContainerPhase_CONTAINER_PHASE_EXITED
	c.exit = exit
	c.exitedAt = time.Now()
	c.running = map[string]struct{}{}
	l := c.link
	c.mu.Unlock()
	c.cancelWork()
	if l != nil {
		l.close(0)
	}
	c.log.Info("container exited", "reason", exit.GetReason(), "message", exit.GetMessage())
	c.report()
}

// cleanup removes the Docker container and the container's directories once.
func (c *container) cleanup(ctx context.Context) {
	c.mu.Lock()
	if c.cleaned || c.phase != hostproto.ContainerPhase_CONTAINER_PHASE_EXITED {
		c.mu.Unlock()
		return
	}
	c.cleaned = true
	c.mu.Unlock()
	if c.requests != nil {
		c.requests.CloseIdleConnections()
	}
	c.control.CloseIdleConnections()
	c.ports.CloseIdleConnections()
	c.a.volumes.release(c.id)
	c.a.volumes.releaseBuckets(ctx, c.id)
	if err := c.a.removeContainer(ctx, c.dockerName()); err != nil {
		c.log.Warn("removing docker container failed", "error", err)
	}
	// Disk leases live outside the container directory, for the release
	// loop to retry until the server accepts.
	c.a.requestRelease()
	for _, dir := range []string{c.dir, c.linkDir()} {
		if err := os.RemoveAll(dir); err != nil {
			c.log.Warn("removing container directory failed", "dir", dir, "error", err)
		}
	}
}

func (c *container) expired(now time.Time) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.phase == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED && c.cleaned && now.Sub(c.exitedAt) > exitRetention
}

// detach stops serving the link when the agent shuts down. The container
// keeps running.
func (c *container) detach() {
	c.mu.Lock()
	l := c.link
	c.mu.Unlock()
	if l != nil {
		l.close(0)
	}
}

// failVolume stops a container whose volume mount died; its exit reports
// why.
func (c *container) failVolume(ctx context.Context, reason string) {
	c.mu.Lock()
	if c.phase == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED {
		c.mu.Unlock()
		return
	}
	c.volumeLost = reason
	c.mu.Unlock()
	if err := c.a.stopDocker(ctx, c.dockerName(), 0); err != nil {
		c.log.Warn("stopping a container whose volume mount died failed", "error", err)
	}
}

func (c *container) hasExited() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.phase == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED
}

// reload restarts the container's runners once their work finishes, so new
// workspace source takes effect.
func (c *container) reload() {
	c.mu.Lock()
	l := c.link
	c.mu.Unlock()
	if l != nil {
		l.enqueue(&hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Reload{Reload: &hostproto.Reload{}}})
	}
}

// servesHTTP reports whether the container's workers take requests now. A
// draining container still gets them; its supervisor answers busy.
func (c *container) servesHTTP() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.http != nil && c.phase == hostproto.ContainerPhase_CONTAINER_PHASE_READY
}

func (c *container) isStopping() bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.stopping
}

// stop drains the container, then stops it after grace. A container that is
// still preparing stops preparing.
func (c *container) stop(grace time.Duration) {
	c.mu.Lock()
	if c.phase == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED {
		c.mu.Unlock()
		c.report()
		return
	}
	if c.stopping {
		c.mu.Unlock()
		return
	}
	c.stopping = true
	c.grace = grace
	started := c.started
	c.mu.Unlock()
	c.cancelClaims()
	// A build has nothing to drain; ending its work ends the builder.
	if !started || c.isBuild {
		c.cancelWork()
		return
	}
	c.a.goOwned(c.stopRunning)
}

func (c *container) stopRunning(ctx context.Context) {
	c.mu.Lock()
	grace, l := c.grace, c.link
	c.mu.Unlock()
	l.enqueue(&hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Drain{Drain: &hostproto.Drain{}}})
	timer := time.NewTimer(grace)
	defer timer.Stop()
	select {
	case <-c.gone:
		return
	case <-ctx.Done():
		return
	case <-timer.C:
	}
	c.log.Info("drain grace expired; stopping container", "grace", grace)
	if err := c.a.stopDocker(ctx, c.dockerName(), stopKillSeconds); err != nil {
		c.log.Warn("stopping docker container failed", "error", err)
	}
}

func (c *container) onSupervisorMessage(ctx context.Context, m *hostproto.SupervisorMessage) {
	switch body := m.GetBody().(type) {
	case *hostproto.SupervisorMessage_Ready:
		c.onReady(body.Ready)
	case *hostproto.SupervisorMessage_LoadFailed:
		c.mu.Lock()
		c.loadError = body.LoadFailed.GetError()
		c.mu.Unlock()
		c.log.Warn("handler failed to load", "type", c.loadError.GetType(), "message", c.loadError.GetMessage())
	case *hostproto.SupervisorMessage_Finished:
		c.onFinished(body.Finished)
	case *hostproto.SupervisorMessage_CommandExited:
		c.onCommandExited(body.CommandExited)
	case *hostproto.SupervisorMessage_Output:
		// Output outside an attempt, such as import-time prints and HTTP
		// requests, goes to the container's own log.
		o := body.Output
		_ = c.logs.append(ctx, &hostproto.LogLine{
			AttemptId: o.GetAttemptId(), RequestId: o.GetRequestId(), Stream: o.GetStream(), Data: o.GetData(), Time: o.GetTime(),
		})
	default:
		c.log.Warn("ignoring unknown supervisor message")
	}
}

// onReady marks the container ready. After an agent restart the supervisor
// restates its running attempts, including finished ones it has not yet
// reported, which then occupy slots.
func (c *container) onReady(ready *hostproto.SlotsReady) {
	c.mu.Lock()
	if c.phase == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED {
		c.mu.Unlock()
		return
	}
	running := map[string]struct{}{}
	now := time.Now()
	for _, attempt := range ready.GetRunningAttempts() {
		// A cancel queued behind this report kills the attempt silently.
		if !c.cancelled.has(attempt, now) {
			running[attempt] = struct{}{}
		}
	}
	for _, attempt := range c.link.queuedAttempts() {
		running[attempt] = struct{}{}
	}
	// The server counts an attempt until its completion commits.
	for attempt := range c.completing {
		running[attempt] = struct{}{}
	}
	c.running = running
	changed := c.phase != hostproto.ContainerPhase_CONTAINER_PHASE_READY
	c.phase = hostproto.ContainerPhase_CONTAINER_PHASE_READY
	if changed && !c.created.IsZero() {
		c.startup = append(c.startup, &hostproto.StartupStage{Kind: hostproto.StartupStageKind_STARTUP_STAGE_KIND_RUNTIME,
			StartedAt: timestamppb.New(c.created), FinishedAt: timestamppb.New(now)})
	}
	// HTTP workers take requests from the data connection instead, and
	// pods run a command.
	startClaims := !c.claiming && !c.stopping && c.http == nil && c.pod == nil
	c.claiming = c.claiming || startClaims
	c.mu.Unlock()
	c.signalSlotFree()
	if changed {
		c.log.Info("container ready", "slots", ready.GetSlots(), "running", len(running))
		c.report()
	}
	if startClaims {
		c.a.goOwned(func(context.Context) { c.claimLoop(c.claims) }) //nolint:contextcheck // claims end when the container starts stopping
	}
}

func (c *container) signalSlotFree() {
	select {
	case c.slotFree <- struct{}{}:
	default:
	}
}

func (c *container) freeSlots() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.slots - len(c.running)
}

// claimLoop claims tasks for free slots until the container stops claiming.
func (c *container) claimLoop(ctx context.Context) {
	delay := 100 * time.Millisecond
	for ctx.Err() == nil {
		free := c.freeSlots()
		if free <= 0 {
			select {
			case <-ctx.Done():
				return
			case <-c.slotFree:
			}
			continue
		}
		callCtx, cancel := context.WithTimeout(ctx, (claimWaitSeconds+10)*time.Second)
		response, err := c.a.host.ClaimTasks(callCtx, &hostproto.ClaimTasksRequest{
			ContainerId: c.id,
			MaxTasks:    int32(free), //nolint:gosec // bounded by slots
			WaitSeconds: claimWaitSeconds,
		})
		cancel()
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			level := slog.LevelWarn
			if status.Code(err) == codes.FailedPrecondition {
				// The server no longer lets this container claim, as while
				// it drains; a stop follows.
				level = slog.LevelDebug
			}
			c.log.Log(ctx, level, "claim failed", "error", err, "retry_in", delay)
			if !sleep(ctx, delay) {
				return
			}
			delay = min(2*delay, 5*time.Second)
			continue
		}
		delay = 100 * time.Millisecond
		for _, task := range response.GetTasks() {
			c.dispatch(task)
		}
	}
}

// dispatch hands a claimed attempt to the supervisor unless its cancel
// arrived on the session before the claim response.
func (c *container) dispatch(task *hostproto.ClaimedTask) {
	attempt := task.GetAttemptId()
	c.mu.Lock()
	if c.phase == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED {
		c.mu.Unlock()
		return
	}
	if c.cancelled.has(attempt, time.Now()) {
		c.mu.Unlock()
		c.log.Info("dropping claimed attempt that was already cancelled", "attempt_id", attempt)
		return
	}
	c.running[attempt] = struct{}{}
	c.traces[attempt] = attemptTrace{task: task.GetTaskId(), traceparent: task.GetTraceparent(), started: time.Now()}
	l := c.link
	c.mu.Unlock()
	l.enqueue(&hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Run{Run: &hostproto.RunAttempt{
		TaskId:        task.GetTaskId(),
		AttemptId:     attempt,
		InputEncoding: task.GetInputEncoding(),
		Input:         task.GetInput(),
		Deadline:      task.GetDeadline(),
		AttemptNumber: task.GetAttemptNumber(),
		MaxAttempts:   task.GetMaxAttempts(),
		RootTaskId:    task.GetRootTaskId(),
		ParentTaskId:  task.GetParentTaskId(),
		Dependencies:  task.GetDependencies(),
	}}})
}

// cancelAttempt kills the slot running attempt, or remembers the cancel for
// a claim response still in flight. The slot is free for claims at once; the
// supervisor queues the next attempt until it has restarted.
func (c *container) cancelAttempt(attempt string) {
	c.mu.Lock()
	c.cancelled.add(attempt, time.Now())
	delete(c.running, attempt)
	delete(c.traces, attempt)
	l := c.link
	c.mu.Unlock()
	c.signalSlotFree()
	if l != nil {
		l.enqueue(&hostproto.SupervisorCommand{Body: &hostproto.SupervisorCommand_Cancel{Cancel: &hostproto.CancelSlot{AttemptId: attempt}}})
	}
}

// onFinished completes the attempt after its output, then frees the slot.
// The server counts the attempt against the container's slots until the
// completion commits, so claiming earlier would find no free slot and wait.
// The container is untrusted: only one outcome per running attempt counts.
func (c *container) onFinished(finished *hostproto.AttemptFinished) {
	attempt := finished.GetAttemptId()
	c.mu.Lock()
	_, running := c.running[attempt]
	_, completing := c.completing[attempt]
	if !running || completing {
		c.mu.Unlock()
		c.log.Debug("ignoring outcome for an attempt that awaits none", "attempt_id", attempt, "completing", completing)
		return
	}
	c.completing[attempt] = struct{}{}
	c.mu.Unlock()
	seq := c.logs.mark()
	c.completions.Add(1)
	c.a.goOwned(func(context.Context) {
		defer c.completions.Done()
		c.complete(c.work, seq, finished) //nolint:contextcheck // delivery lasts as long as the container's work
		c.mu.Lock()
		delete(c.running, attempt)
		delete(c.completing, attempt)
		c.mu.Unlock()
		c.signalSlotFree()
	})
}

// complete delivers an outcome after its output, retrying transient failures
// until ctx ends with the reported exit or the agent's stop. The call in
// flight then still finishes; an outcome never delivered is left to the
// server's reconciliation.
func (c *container) complete(ctx context.Context, seq uint64, finished *hostproto.AttemptFinished) {
	c.logs.waitFlushed(ctx, seq)
	request := &hostproto.CompleteTaskRequest{ContainerId: c.id, AttemptId: finished.GetAttemptId()}
	switch outcome := finished.GetOutcome().(type) {
	case *hostproto.AttemptFinished_Success:
		request.Outcome = &hostproto.CompleteTaskRequest_Success{Success: outcome.Success}
	case *hostproto.AttemptFinished_Failure:
		request.Outcome = &hostproto.CompleteTaskRequest_Failure{Failure: outcome.Failure}
	default:
		c.log.Error("attempt finished without an outcome", "attempt_id", finished.GetAttemptId())
		return
	}
	ctx, span := c.attemptSpan(ctx, finished.GetAttemptId())
	defer span.End()
	delay := 100 * time.Millisecond
	for {
		callCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), completeCallTimeout)
		_, err := c.a.host.CompleteTask(callCtx, request)
		cancel()
		switch {
		case err == nil:
			return
		case status.Code(err) == codes.FailedPrecondition:
			c.log.Info("discarding stale attempt outcome", "attempt_id", finished.GetAttemptId())
			return
		case !retryable(err) || ctx.Err() != nil:
			c.log.Error("completing attempt failed", "attempt_id", finished.GetAttemptId(), "error", err)
			return
		}
		c.log.Warn("completing attempt failed; retrying", "attempt_id", finished.GetAttemptId(), "error", err, "retry_in", delay)
		// A stop during the wait leaves one last call.
		sleep(ctx, delay)
		delay = min(2*delay, maxCompleteBackoff)
	}
}

// attemptSpan records the attempt as a span from its dispatch, in the trace
// of the request that submitted it, so the completion call and the server's
// handling of it join that trace.
func (c *container) attemptSpan(ctx context.Context, attempt string) (context.Context, trace.Span) {
	c.mu.Lock()
	t, ok := c.traces[attempt]
	delete(c.traces, attempt)
	c.mu.Unlock()
	if !ok || c.a.cfg.Telemetry == nil {
		return ctx, noop.Span{}
	}
	ctx = telemetry.WithTraceParent(ctx, t.traceparent)
	return c.a.cfg.Telemetry.Tracer().Start(ctx, "attempt", trace.WithTimestamp(t.started), trace.WithAttributes(
		attribute.String("lazycloud."+telemetry.KeyTask, t.task),
		attribute.String("lazycloud."+telemetry.KeyAttempt, attempt),
		attribute.String("lazycloud."+telemetry.KeyContainer, c.id),
		attribute.String("lazycloud."+telemetry.KeyHost, c.a.identity.HostID),
	))
}

// cancelledAttempts remembers recent cancels, oldest first.
type cancelledAttempts struct {
	at    map[string]time.Time
	order []string
}

func (s *cancelledAttempts) add(attempt string, now time.Time) {
	if _, known := s.at[attempt]; known {
		return
	}
	for len(s.order) > 0 && (len(s.order) >= maxCancelled || now.Sub(s.at[s.order[0]]) >= cancelledRetention) {
		delete(s.at, s.order[0])
		s.order = s.order[1:]
	}
	s.at[attempt] = now
	s.order = append(s.order, attempt)
}

func (s *cancelledAttempts) has(attempt string, now time.Time) bool {
	at, known := s.at[attempt]
	return known && now.Sub(at) < cancelledRetention
}
