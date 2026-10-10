package hostsession

import (
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/secrets"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// inboxSize bounds host messages read ahead of processing. A full inbox
	// stops reading, and gRPC flow control then slows the host.
	inboxSize = 32
	// stopGraceSeconds is the time a stopped container gets to exit.
	stopGraceSeconds = 10
)

type session struct {
	server *Server
	stream grpc.BidiStreamingServer[hostproto.HostMessage, hostproto.ServerMessage]
	host   compute.HostID
	// opened is when the session opened.
	opened time.Time
	// sent holds the ids of derived commands this session already sent, so
	// each is sent once per session. It keeps only ids still derived.
	sent map[string]bool
	// grants holds the expiry of the storage grant sent per workspace.
	grants map[identity.WorkspaceID]time.Time
	// layers holds the layer grants sent per image reference, and
	// layersListed whether the host's live references were listed yet.
	layers       map[string]layerGrant
	layersListed bool
	// live holds the containers the host runs as far as this session knows:
	// reported and not exited, or started. A stop decided elsewhere, such as
	// a machine removal or a preemption, reaches the host through it.
	live map[execution.ContainerID]bool
	// networks holds the newest policy version recorded per container, so
	// a report restating it writes nothing.
	networks map[execution.ContainerID]int32
	// reserve is the PrepareReserve awaiting its answer.
	reserve *reserveAttempt
	// builds wakes the session when an image build a start waits for, or
	// a platform image conversion, changes.
	builds watch
	// platform is what the session sent for the platform images the Hello
	// named.
	platform platformState
	// replicas wakes the session when layers are confirmed in region, its
	// host's region with a copy of the layer bucket, once a grant names
	// the region; a confirmation that outdates no grant costs no sync.
	replicas watch
	region   string
	// again wakes the session for a wake-up a replaced build watch held.
	again chan struct{}
	// starts holds the open spans of starts not sent yet.
	starts map[execution.ContainerID]*pendingStart
}

// watch is one subscription to ChannelImageBuild keys, replaced when the
// keys change. A change committed before a key's subscription reaches the
// session at its next touch.
type watch struct {
	keys   []string
	wake   <-chan struct{}
	cancel func()
}

// set watches keys, sorted, and nothing when there are none. The new
// subscription starts before the old one ends, and set reports whether the
// old one held a wake-up, which the caller acts on as if the new one woke.
func (w *watch) set(listener *database.Listener, keys ...string) bool {
	if slices.Equal(keys, w.keys) {
		return false
	}
	cancel, wake := w.cancel, w.wake
	w.keys, w.wake, w.cancel = keys, nil, nil
	if len(keys) > 0 {
		w.wake, w.cancel = listener.Subscribe(database.ChannelImageBuild, keys...)
	}
	if cancel == nil {
		return false
	}
	cancel()
	select {
	case <-wake:
		return true
	default:
		return false
	}
}

// recordNetwork records a newly reported applied policy version.
func (sess *session) recordNetwork(ctx context.Context, container execution.ContainerID, report *hostproto.ContainerReport) error {
	version := report.GetNetworkVersion()
	if version <= sess.networks[container] {
		return nil
	}
	if err := sess.server.execution.NetworkApplied(ctx, sess.host, container, version, report.GetNetworkError()); err != nil {
		return err //nolint:wrapcheck // The caller maps it.
	}
	sess.networks[container] = version
	return nil
}

// Session serves a host's control stream. Hello opens a session epoch and
// reconciles the host's containers. The session then sends the commands
// durable state implies whenever the host is woken, applies container
// reports, and touches the host every TouchInterval. It ends when a newer
// session supersedes it, the host disconnects or the server shuts down.
func (s *Server) Session(stream grpc.BidiStreamingServer[hostproto.HostMessage, hostproto.ServerMessage]) error {
	ctx := stream.Context()
	host := hostFrom(ctx)
	first, err := stream.Recv()
	if err != nil {
		return fmt.Errorf("receive hello: %w", err)
	}
	hello := first.GetHello()
	if hello == nil {
		return status.Error(codes.InvalidArgument, "the first message must be Hello")
	}
	if max(len(hello.GetPlatformImages()), len(hello.GetRunningPlatformImages())) > maxHelloPlatformImages {
		return status.Errorf(codes.InvalidArgument, "a host names at most %d platform images and %d running copies", maxHelloPlatformImages, maxHelloPlatformImages)
	}
	reports := make([]execution.ContainerReport, 0, len(hello.GetContainers()))
	for _, c := range hello.GetContainers() {
		report, err := reportIn(c)
		if err != nil {
			return err
		}
		reports = append(reports, report)
	}

	// Subscribe before reading durable state so no change is missed.
	wake, unsubscribe := s.listener.Subscribe(database.ChannelHost, host.String())
	defer unsubscribe()
	epoch, err := s.compute.OpenSession(ctx, host, sessionOpenIn(hello))
	if err != nil {
		return s.grpcError(ctx, err)
	}
	sess := &session{server: s, stream: stream, host: host, opened: time.Now(), again: make(chan struct{}, 1), sent: map[string]bool{}, live: map[execution.ContainerID]bool{},
		networks: map[execution.ContainerID]int32{}, grants: map[identity.WorkspaceID]time.Time{}, layers: map[string]layerGrant{},
		platform: platformState{
			named:   slices.Compact(slices.Sorted(slices.Values(hello.GetPlatformImages()))),
			running: slices.Compact(slices.Sorted(slices.Values(hello.GetRunningPlatformImages()))),
			answers: map[string]layerGrant{}, failed: map[string]string{},
		},
		starts: map[execution.ContainerID]*pendingStart{}}
	defer sess.builds.set(s.listener)
	defer sess.replicas.set(s.listener)
	defer func() {
		// Ending the starts stores their waits so far.
		ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		defer cancel()
		sess.endStarts(ctx, nil)
	}()
	for _, r := range reports {
		sess.observe(r)
	}
	update, err := s.compute.UpdateFor(ctx, host)
	if err != nil {
		return s.grpcError(ctx, err)
	}
	if update != nil {
		if err := sess.sendUpdate(ctx, update); err != nil {
			return err
		}
	}
	actions, err := s.execution.ReconcileHost(ctx, host, reports)
	if err != nil {
		return s.grpcError(ctx, err)
	}
	for _, c := range hello.GetContainers() {
		s.recordStartup(ctx, host, c)
	}
	if err := sess.sendActions(actions); err != nil {
		return err
	}
	if err := sess.sync(ctx); err != nil {
		return err
	}

	inbox := make(chan *hostproto.HostMessage, inboxSize)
	received := make(chan error, 1)
	// Recv unblocks when the RPC ends, which happens once this handler
	// returns; Wait on the server waits for it.
	s.receivers.Go(func() {
		for {
			msg, err := stream.Recv()
			if err != nil {
				received <- err
				return
			}
			select {
			case inbox <- msg:
			case <-ctx.Done():
				return
			}
		}
	})

	touched := func() error {
		current, err := s.compute.Touch(ctx, host, epoch)
		if err != nil {
			return s.grpcError(ctx, err)
		}
		if !current {
			return status.Error(codes.Aborted, "a newer session replaced this one")
		}
		return nil
	}
	touch := time.NewTicker(s.config.TouchInterval)
	defer touch.Stop()
	for {
		select {
		case <-s.lifetime.Done():
			return status.Error(codes.Unavailable, "the server is shutting down")
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case err := <-received:
			if errors.Is(err, io.EOF) {
				return nil
			}
			return fmt.Errorf("receive: %w", err)
		case msg := <-inbox:
			if err := sess.handle(ctx, msg); err != nil {
				return err
			}
			continue
		case <-sess.builds.wake:
		case <-sess.again:
		case <-sess.replicas.wake:
			if !sess.outdateUnconfirmed() {
				continue
			}
		case <-wake:
			// A removal wakes the session too; touching at once makes the
			// agent find its credential revoked without waiting for a touch.
			if err := touched(); err != nil {
				return err
			}
		case <-touch.C:
			if err := touched(); err != nil {
				return err
			}
			// A release published while the session is open, or widened
			// to this host, reaches it here.
			update, err := s.compute.UpdateFor(ctx, host)
			if err != nil {
				return s.grpcError(ctx, err)
			}
			if update != nil && !sess.sent["update:"+update.Version] {
				if err := sess.sendUpdate(ctx, update); err != nil {
					return err
				}
			}
		}
		if err := sess.sync(ctx); err != nil {
			return err
		}
	}
}

func (sess *session) handle(ctx context.Context, msg *hostproto.HostMessage) error {
	switch body := msg.GetBody().(type) {
	case *hostproto.HostMessage_Container:
		report, err := reportIn(body.Container)
		if err != nil {
			return err
		}
		sess.observe(report)
		actions, err := sess.server.execution.ApplyReport(ctx, sess.host, report)
		if err != nil {
			return sess.server.grpcError(ctx, err)
		}
		sess.server.recordStartup(ctx, sess.host, body.Container)
		if err := sess.recordNetwork(ctx, report.Container, body.Container); err != nil {
			return sess.server.grpcError(ctx, err)
		}
		if failed := body.Container.GetRestoreFailed(); failed != "" {
			if snapshot, err := uuid.Parse(failed); err == nil {
				if err := sess.server.execution.RestoreFailed(ctx, snapshot, "the host started the container cold"); err != nil {
					return sess.server.grpcError(ctx, err)
				}
			}
		}
		return sess.sendActions(actions)
	case *hostproto.HostMessage_Metrics:
		sess.server.offerMetrics(sess.host, body.Metrics)
		return nil
	case *hostproto.HostMessage_Interruption:
		at := time.Now()
		if body.Interruption.GetReclaimAt() != nil {
			at = body.Interruption.GetReclaimAt().AsTime()
		}
		if err := sess.server.compute.ReportInterruption(ctx, sess.host, body.Interruption.GetReason(), at); err != nil {
			return sess.server.grpcError(ctx, err)
		}
		return nil
	case *hostproto.HostMessage_ReserveReady:
		return sess.answerReserve(ctx, body.ReserveReady)
	case *hostproto.HostMessage_StartupTrace:
		sess.recordTrace(ctx, body.StartupTrace)
		return nil
	case *hostproto.HostMessage_Traces:
		sess.server.config.Traces.Offer(sess.host.String(), body.Traces.GetOtlp())
		return nil
	case *hostproto.HostMessage_Ack:
		// Acknowledgement is receipt only; the following report shows the
		// outcome.
		return nil
	case *hostproto.HostMessage_Hello:
		return status.Error(codes.InvalidArgument, "Hello is only the first message")
	}
	return status.Error(codes.InvalidArgument, "empty host message")
}

// syncCache holds what one sync read and signed per image, so the replicas
// of one image cost one read and one signing, the images of the starts it
// sent, and the ChannelImageBuild keys of the waits it found.
type syncCache struct {
	pulls   map[pullKey]pulled
	layers  map[string]issuedLayers
	traces  map[traceKey]startTrace
	started []string
	waits   map[string]bool
}

// sync derives the host's commands and sends those not yet sent.
func (sess *session) sync(ctx context.Context) error {
	commands, err := sess.server.execution.HostCommands(ctx, sess.host)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	derived := map[string]bool{}
	cache := &syncCache{pulls: map[pullKey]pulled{}, layers: map[string]issuedLayers{}, traces: map[traceKey]startTrace{}, waits: map[string]bool{}}
	// Builds, volume mounts and network holders run the platform images,
	// so they go first.
	if err := sess.syncPlatform(ctx, cache); err != nil {
		return err
	}
	// The images of the starts not sent yet resolve first, so their layers
	// are signed together.
	var references []string
	for _, start := range commands.Start {
		if !sess.sent["start:"+start.Container.String()] {
			if pull, err := sess.imagePull(sess.startContext(ctx, start), cache, start.Workspace, start.Spec.Image); err == nil {
				references = append(references, pull.Reference)
			}
		}
	}
	sess.signLayers(ctx, cache, references)
	starting := map[execution.ContainerID]bool{}
	for _, start := range commands.Start {
		id := "start:" + start.Container.String()
		if !sess.sent[id] {
			starting[start.Container] = true
			waiting, err := sess.sendStart(ctx, cache, id, start)
			if err != nil {
				return err
			}
			// A start waiting for its image is not sent yet.
			derived[id] = !waiting
		} else {
			derived[id] = true
		}
	}
	sess.endStarts(ctx, starting)
	if sess.builds.set(sess.server.listener, slices.Sorted(maps.Keys(cache.waits))...) {
		select {
		case sess.again <- struct{}{}:
		default:
		}
	}
	if err := sess.server.images.RecordUses(ctx, cache.started); err != nil {
		return sess.server.grpcError(ctx, err)
	}
	if err := sess.syncBuilds(ctx, derived); err != nil {
		return err
	}
	for _, stop := range commands.Stop {
		id := "stop:" + stop.Container.String()
		derived[id] = true
		if !sess.sent[id] {
			if err := sess.send(stopMessage(id, stop.Container, stop.Grace)); err != nil {
				return err
			}
		}
	}
	for _, cancel := range commands.Cancel {
		id := "cancel:" + cancel.Attempt.String()
		derived[id] = true
		if !sess.sent[id] {
			if err := sess.send(cancelMessage(id, cancel)); err != nil {
				return err
			}
		}
	}
	if err := sess.syncWorkloads(ctx, commands, derived); err != nil {
		return err
	}
	if err := sess.syncStopped(ctx, derived); err != nil {
		return err
	}
	for id := range sess.sent {
		if strings.HasPrefix(id, "update:") {
			derived[id] = true
		}
	}
	sess.sent = derived
	if err := sess.syncReserve(ctx); err != nil {
		return err
	}
	if err := sess.refreshGrants(ctx); err != nil {
		return err
	}
	if err := sess.refreshLayers(ctx, cache); err != nil {
		return err
	}
	return nil
}

// sendStart sends start under id once its image is published with
// converted layers, or reports that it waits for its image. A start that
// can never be built fails, and one whose container no longer starts here
// is dropped. A returned error ends the session, and the start is built
// again.
func (sess *session) sendStart(ctx context.Context, cache *syncCache, id string, start execution.StartCommand) (bool, error) {
	ctx = sess.startContext(ctx, start)
	msg, grant, err := sess.startMessage(ctx, cache, id, start)
	var building *images.BuildWaitError
	var converting *images.PlatformWaitError
	switch {
	case errors.As(err, &building):
		sess.waiting(ctx, start.Container, "build", building.Traceparent)
		cache.waits[building.Build.String()] = true
		return true, nil
	case errors.As(err, &converting):
		sess.waiting(ctx, start.Container, "platform_conversion", converting.Traceparent)
		sess.server.convertPlatform(converting.Reference, converting.Architecture, telemetry.TraceParentOf(ctx)) //nolint:contextcheck // Conversions run under the server's lifetime.
		cache.waits[images.PlatformConverted] = true
		return true, nil
	case errors.Is(err, images.ErrNotReady) || errors.Is(err, images.ErrNotConverted):
		// With no build to follow, the next touch's pull starts or joins
		// the conversion.
		sess.waiting(ctx, start.Container, "image", "")
		return true, nil
	}
	if reason, permanent := permanentStartFailure(err); permanent {
		// The container fails like a failed preparation and stops being
		// derived; the host's other containers are unaffected.
		sess.server.logger.WarnContext(ctx, "container cannot start", "host", sess.host.String(), "container", start.Container.String(), "error", err)
		sess.endStart(ctx, start.Container, reason, startDone)
		return false, sess.server.grpcError(ctx, sess.server.execution.StartFailed(ctx, sess.host, start.Container, reason))
	}
	if err != nil {
		return false, sess.server.grpcError(ctx, err)
	}
	image := msg.GetStart().GetImage()
	// The host may read the image's layers while the container is live.
	recorded, err := sess.server.execution.RecordImageReference(ctx, sess.host, start.Container, image)
	if err != nil {
		return false, sess.server.grpcError(ctx, err)
	}
	if !recorded {
		sess.endStart(ctx, start.Container, "", startDropped)
		return false, nil
	}
	if usesWorkspaceBucket(msg.GetStart()) {
		if err := sess.ensureGrant(ctx, start.Workspace); err != nil {
			return false, err
		}
	}
	msg.GetStart().Traceparent = telemetry.TraceParentOf(ctx)
	if err := sess.send(msg); err != nil {
		return false, err
	}
	sess.endStart(ctx, start.Container, "", startDone)
	sess.live[start.Container] = true
	sess.layers[image] = grant
	cache.started = append(cache.started, image)
	return false, nil
}

// observe tracks whether the host still runs a reported container.
func (sess *session) observe(report execution.ContainerReport) {
	if report.Phase == execution.ReportExited {
		delete(sess.live, report.Container)
		return
	}
	sess.live[report.Container] = true
}

// syncStopped sends a stop for every container the host runs that is
// already stopped.
func (sess *session) syncStopped(ctx context.Context, derived map[string]bool) error {
	ids := make([]execution.ContainerID, 0, len(sess.live))
	for id := range sess.live {
		ids = append(ids, id)
	}
	stopped, err := sess.server.execution.StoppedAmong(ctx, ids)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	for _, container := range stopped {
		id := "stop:" + container.String()
		derived[id] = true
		if !sess.sent[id] {
			if err := sess.send(stopMessage(id, container, 0)); err != nil {
				return err
			}
		}
	}
	return nil
}

// sendActions sends the commands a report implied. They are not derived
// from durable state, so a later report of the same thing sends them again.
func (sess *session) sendActions(actions execution.ReportActions) error {
	for _, container := range actions.Stop {
		if err := sess.send(stopMessage("stop:"+container.String(), container, 0)); err != nil {
			return err
		}
	}
	for _, cancel := range actions.Cancel {
		if err := sess.send(cancelMessage("cancel:"+cancel.Attempt.String(), cancel)); err != nil {
			return err
		}
	}
	return nil
}

func (sess *session) send(msg *hostproto.ServerMessage) error {
	if err := sess.stream.Send(msg); err != nil {
		return fmt.Errorf("send command %s: %w", msg.GetCommandId(), err)
	}
	sess.sent[msg.GetCommandId()] = true
	return nil
}

// startMessage builds start's command and the record of the layer grants it
// carries. The image comes first: a start that waits for it signs, mounts
// and resolves nothing.
func (sess *session) startMessage(ctx context.Context, cache *syncCache, id string, start execution.StartCommand) (*hostproto.ServerMessage, layerGrant, error) {
	s := sess.server
	image, err := sess.imagePull(ctx, cache, start.Workspace, start.Spec.Image)
	if err != nil {
		return nil, layerGrant{}, err
	}
	layers, grant, err := sess.grantLayers(ctx, cache, image.Reference)
	if err != nil {
		return nil, layerGrant{}, err
	}
	url, expires, err := s.storage.SourceURL(ctx, start.Workspace, start.Source)
	if err != nil {
		return nil, layerGrant{}, err
	}
	version := string(start.Spec.Image.PythonVersion)
	env := map[string]string{}
	if start.Spec.Environment != nil {
		env = *start.Spec.Environment
	}
	volumes, err := s.volumeMounts(ctx, start)
	if err != nil {
		return nil, layerGrant{}, err
	}
	var diskLimit int64
	if start.Spec.Resources.DiskMib != nil {
		diskLimit = int64(*start.Spec.Resources.DiskMib) << 20
	}
	var names []string
	if start.Spec.Secrets != nil {
		names = *start.Spec.Secrets
	}
	secretValues, err := s.config.Secrets.Resolve(ctx, start.Workspace, names)
	if err != nil {
		return nil, layerGrant{}, err
	}
	trace := s.startTrace(ctx, cache, start.Workspace, image.Reference)
	restore, err := s.restoreOut(ctx, start.Container)
	if err != nil {
		return nil, layerGrant{}, err
	}
	var function *hostproto.FunctionWorkload
	var pod *hostproto.PodWorkload
	var http *hostproto.HttpServing
	if start.Spec.Pod != nil || start.Purpose == execution.PurposeShell {
		// A shell container of an endpoint serves no HTTP.
		if pod, err = s.podWorkload(ctx, start); err != nil {
			return nil, layerGrant{}, err
		}
	} else {
		http = httpServing(start.Spec)
		function = &hostproto.FunctionWorkload{
			Handler:   deref(start.Spec.Handler),
			Slots:     int32(start.Slots), //nolint:gosec // The schema caps concurrency at 256.
			InProcess: start.Spec.InProcess != nil && *start.Spec.InProcess,
			Hooks:     hooksOut(start.Spec.LifecycleHooks),
		}
	}
	return &hostproto.ServerMessage{CommandId: id, Body: &hostproto.ServerMessage_Start{Start: &hostproto.StartContainer{
		ContainerId:   start.Container.String(),
		Image:         image.Reference,
		ImageAuth:     registryAuthOut(image.Auth),
		ImagePlatform: image.Platform,
		PythonVersion: version,
		Source:        &hostproto.Source{Sha256: start.Source.String(), Url: url, UrlExpiresAt: timestamppb.New(expires)},
		Resources: &hostproto.Resources{
			CpuMillis: int64(start.CPUMillis), MemoryBytes: start.MemoryBytes,
			CpuLimitMillis: int64(start.CPULimitMillis), MemoryLimitBytes: start.MemoryLimitBytes,
			DiskLimitBytes: diskLimit,
			GpuCount:       gpusOf(start.Spec.Resources),
		},
		Function:       function,
		Pod:            pod,
		Docker:         start.Spec.DockerEnabled != nil && *start.Spec.DockerEnabled,
		Restore:        restore,
		Checkpointable: execution.Checkpointable(start.Kind, start.Purpose, start.Spec),
		Disks:          disksOut(start.Spec),
		Http:           http,
		Environment:    env,
		Volumes:        volumes,
		Secrets:        secretValues,
		Workspace:      start.WorkspaceName,
		Layers:         layers,
		Prefetch:       trace.prefetch,
		RecordTrace:    trace.record,
	}}}, grant, nil
}

func hooksOut(h *apitypes.LifecycleHooks) *hostproto.LifecycleHooks {
	if h == nil {
		return &hostproto.LifecycleHooks{}
	}
	refs := func(r *apitypes.HookReferences) []string {
		if r == nil {
			return nil
		}
		return *r
	}
	return &hostproto.LifecycleHooks{
		OnStart: refs(h.OnStart), OnRunning: refs(h.OnRunning), OnSuccess: refs(h.OnSuccess),
		OnError: refs(h.OnError), OnRetry: refs(h.OnRetry), OnFailure: refs(h.OnFailure), OnFinish: refs(h.OnFinish),
	}
}

// stopMessage stops a container after grace, or stopGraceSeconds when grace
// is zero.
func stopMessage(id string, container execution.ContainerID, grace time.Duration) *hostproto.ServerMessage {
	seconds := int32(stopGraceSeconds)
	if grace > 0 {
		seconds = int32(min(grace.Seconds(), 86400))
	}
	return &hostproto.ServerMessage{CommandId: id, Body: &hostproto.ServerMessage_Stop{Stop: &hostproto.StopContainer{
		ContainerId: container.String(), GraceSeconds: seconds,
	}}}
}

// httpServing configures an HTTP workload's workers, or nil for a task
// workload. Each worker admits the spec's concurrency.
func httpServing(spec apitypes.WorkloadSpec) *hostproto.HttpServing {
	if spec.Http == nil {
		return nil
	}
	concurrency := 1
	if spec.Concurrency != nil {
		concurrency = *spec.Concurrency
	}
	kind := hostproto.HttpKind_HTTP_KIND_ENDPOINT
	switch spec.Http.Kind {
	case apitypes.HttpKindAsgi:
		kind = hostproto.HttpKind_HTTP_KIND_ASGI
	case apitypes.HttpKindRealtime:
		kind = hostproto.HttpKind_HTTP_KIND_REALTIME
	case apitypes.HttpKindEndpoint:
	}
	return &hostproto.HttpServing{Kind: kind, Concurrency: int32(concurrency)} //nolint:gosec // The schema caps concurrency at 256.
}

func cancelMessage(id string, cancel execution.CancelCommand) *hostproto.ServerMessage {
	reason := hostproto.CancelReason_CANCEL_REASON_CANCELLED
	if cancel.Reason == execution.AttemptTimedOut {
		reason = hostproto.CancelReason_CANCEL_REASON_TIMED_OUT
	}
	return &hostproto.ServerMessage{CommandId: id, Body: &hostproto.ServerMessage_Cancel{Cancel: &hostproto.CancelAttempt{
		ContainerId: cancel.Container.String(), AttemptId: cancel.Attempt.String(), Reason: reason,
	}}}
}

func reportIn(c *hostproto.ContainerReport) (execution.ContainerReport, error) {
	container, err := parseContainer(c.GetContainerId())
	if err != nil {
		return execution.ContainerReport{}, err
	}
	if c.GetObservedAt() == nil {
		return execution.ContainerReport{}, status.Error(codes.InvalidArgument, "a container report needs observed_at")
	}
	report := execution.ContainerReport{Container: container, ObservedAt: c.GetObservedAt().AsTime()}
	switch c.GetPhase() {
	case hostproto.ContainerPhase_CONTAINER_PHASE_PREPARING:
		report.Phase = execution.ReportPreparing
	case hostproto.ContainerPhase_CONTAINER_PHASE_STARTING:
		report.Phase = execution.ReportStarting
	case hostproto.ContainerPhase_CONTAINER_PHASE_READY:
		report.Phase = execution.ReportReady
	case hostproto.ContainerPhase_CONTAINER_PHASE_EXITED:
		report.Phase = execution.ReportExited
		exit := exitIn(c.GetExit())
		report.Exit = &exit
	case hostproto.ContainerPhase_CONTAINER_PHASE_UNSPECIFIED:
		return execution.ContainerReport{}, status.Error(codes.InvalidArgument, "a container report needs a phase")
	}
	for _, a := range c.GetRunningAttempts() {
		id, err := uuid.Parse(a)
		if err != nil {
			return execution.ContainerReport{}, status.Error(codes.InvalidArgument, "running_attempts holds a non-UUID")
		}
		report.Running = append(report.Running, execution.AttemptID(id))
	}
	return report, nil
}

func exitIn(e *hostproto.ContainerExit) execution.ContainerExit {
	message := e.GetMessage()
	// A load error's message is the supervisor's own account of it, which
	// the exit code adds nothing to.
	if e.GetExitCode() != 0 && e.GetReason() != hostproto.ExitReason_EXIT_REASON_LOAD_ERROR {
		message = fmt.Sprintf("exit code %d: %s", e.GetExitCode(), message)
	}
	exit := execution.ContainerExit{Message: message}
	switch e.GetReason() {
	case hostproto.ExitReason_EXIT_REASON_STOPPED:
		exit.Reason = execution.StopRequested
	case hostproto.ExitReason_EXIT_REASON_LOAD_ERROR:
		exit.Reason = execution.StopLoadError
		exit.LoadError = &execution.Failure{
			Kind: execution.FailureLoadError, Type: e.GetError().GetType(),
			Message: e.GetError().GetMessage(), Traceback: e.GetError().GetTraceback(),
		}
		if exit.LoadError.Message == "" {
			exit.LoadError.Message = message
		}
	case hostproto.ExitReason_EXIT_REASON_START_FAILED:
		exit.Reason = execution.StopStartFailed
	case hostproto.ExitReason_EXIT_REASON_OUT_OF_MEMORY:
		exit.Reason = execution.StopOutOfMemory
	case hostproto.ExitReason_EXIT_REASON_EXITED:
		exit.Reason = execution.StopExited
		code := int(e.GetExitCode())
		exit.ExitCode = &code
	case hostproto.ExitReason_EXIT_REASON_CRASHED, hostproto.ExitReason_EXIT_REASON_UNSPECIFIED:
		exit.Reason = execution.StopCrashed
	}
	return exit
}

// sendUpdate tells the agent to install a release and gives its host the
// update window to restart in.
func (sess *session) sendUpdate(ctx context.Context, update *compute.AgentUpdate) error {
	if err := sess.server.compute.UpdateSent(ctx, sess.host); err != nil {
		return sess.server.grpcError(ctx, err)
	}
	return sess.send(updateMessage(update))
}

// permanentStartFailure says whether err means the start can never be
// built, and the reason the container's owner is shown.
func permanentStartFailure(err error) (string, bool) {
	var missing *secrets.NotFoundError
	var unreadable *secrets.UnreadableError
	var unconvertible *images.ConversionError
	switch {
	case err == nil:
		return "", false
	case errors.As(err, &missing):
		return missing.Error(), true
	case errors.As(err, &unreadable):
		return "secret " + unreadable.Name + " cannot be read", true
	case errors.Is(err, errImageUnpinned):
		return "the release's image has no pinned reference; deploy it again", true
	case errors.As(err, &unconvertible):
		return unconvertible.Error(), true
	}
	return "", false
}
