package hostsession

import (
	"context"
	"errors"
	"fmt"
	"io"
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
	"github.com/AmbientWare/lazycloud/internal/secrets"
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
	// sent holds the ids of derived commands this session already sent, so
	// each is sent once per session. It keeps only ids still derived.
	sent map[string]bool
	// grants holds the expiry of the storage grant sent per workspace.
	grants map[identity.WorkspaceID]time.Time
	// live holds the containers the host runs as far as this session knows:
	// reported and not exited, or started. A stop decided elsewhere, such as
	// a machine removal or a preemption, reaches the host through it.
	live map[execution.ContainerID]bool
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
	epoch, err := s.compute.OpenSession(ctx, host, compute.SessionOpen{
		BootID: hello.GetBootId(), Capacity: capacityIn(hello.GetCapacity()), AgentVersion: hello.GetAgentVersion(),
	})
	if err != nil {
		return s.grpcError(ctx, err)
	}
	sess := &session{server: s, stream: stream, host: host, sent: map[string]bool{}, live: map[execution.ContainerID]bool{},
		grants: map[identity.WorkspaceID]time.Time{}}
	for _, r := range reports {
		sess.observe(r)
	}
	update, err := s.compute.UpdateFor(ctx, host, hello.GetAgentVersion(), hello.GetRejectedVersion(), hello.GetUpdatable())
	if err != nil {
		return s.grpcError(ctx, err)
	}
	if update != nil {
		if err := sess.send(updateMessage(update)); err != nil {
			return err
		}
	}
	actions, err := s.execution.ReconcileHost(ctx, host, reports)
	if err != nil {
		return s.grpcError(ctx, err)
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
		case <-wake:
			if err := sess.sync(ctx); err != nil {
				return err
			}
		case <-touch.C:
			current, err := s.compute.Touch(ctx, host, epoch)
			if err != nil {
				return s.grpcError(ctx, err)
			}
			if !current {
				return status.Error(codes.Aborted, "a newer session replaced this one")
			}
			if err := sess.sync(ctx); err != nil {
				return err
			}
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
		return sess.sendActions(actions)
	case *hostproto.HostMessage_Interruption:
		at := time.Now()
		if body.Interruption.GetReclaimAt() != nil {
			at = body.Interruption.GetReclaimAt().AsTime()
		}
		if err := sess.server.compute.ReportInterruption(ctx, sess.host, body.Interruption.GetReason(), at); err != nil {
			return sess.server.grpcError(ctx, err)
		}
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

// sync derives the host's commands and sends those not yet sent.
func (sess *session) sync(ctx context.Context) error {
	commands, err := sess.server.execution.HostCommands(ctx, sess.host)
	if err != nil {
		return sess.server.grpcError(ctx, err)
	}
	derived := map[string]bool{}
	for _, start := range commands.Start {
		id := "start:" + start.Container.String()
		derived[id] = true
		if sess.sent[id] {
			continue
		}
		msg, err := sess.server.startMessage(ctx, id, start)
		var missing *secrets.NotFoundError
		var unreadable *secrets.UnreadableError
		if errors.As(err, &missing) || errors.As(err, &unreadable) {
			// The container cannot start without the secret; it fails like
			// a failed preparation and stops being derived, and the host's
			// other containers are unaffected.
			if err := sess.server.execution.StartFailed(ctx, sess.host, start.Container, err.Error()); err != nil {
				return sess.server.grpcError(ctx, err)
			}
			continue
		}
		if err != nil {
			return sess.server.grpcError(ctx, err)
		}
		if usesWorkspaceBucket(msg.GetStart()) {
			if err := sess.ensureGrant(ctx, start.Workspace); err != nil {
				return err
			}
		}
		if err := sess.send(msg); err != nil {
			return err
		}
		sess.live[start.Container] = true
	}
	if err := sess.syncBuilds(ctx, derived); err != nil {
		return err
	}
	for _, stop := range commands.Stop {
		id := "stop:" + stop.Container.String()
		derived[id] = true
		if !sess.sent[id] {
			if err := sess.send(stopMessage(id, stop.Container)); err != nil {
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
	if err := sess.syncStopped(ctx, derived); err != nil {
		return err
	}
	for id := range sess.sent {
		if strings.HasPrefix(id, "update:") {
			derived[id] = true
		}
	}
	sess.sent = derived
	return sess.refreshGrants(ctx)
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
			if err := sess.send(stopMessage(id, container)); err != nil {
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
		if err := sess.send(stopMessage("stop:"+container.String(), container)); err != nil {
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

func (s *Server) startMessage(ctx context.Context, id string, start execution.StartCommand) (*hostproto.ServerMessage, error) {
	url, expires, err := s.storage.SourceURL(ctx, start.Workspace, start.Source)
	if err != nil {
		return nil, err
	}
	version := string(start.Spec.Image.PythonVersion)
	env := map[string]string{}
	if start.Spec.Environment != nil {
		env = *start.Spec.Environment
	}
	volumes, err := s.volumeMounts(ctx, start)
	if err != nil {
		return nil, err
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
		return nil, err
	}
	image, err := s.imagePull(ctx, start.Spec.Image)
	if err != nil {
		return nil, err
	}
	return &hostproto.ServerMessage{CommandId: id, Body: &hostproto.ServerMessage_Start{Start: &hostproto.StartContainer{
		ContainerId:   start.Container.String(),
		Image:         image.Reference,
		ImageAuth:     registryAuthOut(image.Auth),
		ImagePlatform: image.Platform,
		PythonVersion: version,
		Source:        &hostproto.Source{Sha256: start.Source.String(), Url: url, UrlExpiresAt: timestamppb.New(expires)},
		Resources: &hostproto.Resources{
			CpuMillis: start.CPUMillis, MemoryBytes: start.MemoryBytes,
			CpuLimitMillis: start.CPULimitMillis, MemoryLimitBytes: start.MemoryLimitBytes,
			DiskLimitBytes: diskLimit,
			GpuCount: gpusOf(start.Spec.Resources),
		},
		Function: &hostproto.FunctionWorkload{
			Handler:   start.Spec.Handler,
			Slots:     int32(start.Slots), //nolint:gosec // The schema caps concurrency at 256.
			InProcess: start.Spec.InProcess != nil && *start.Spec.InProcess,
			Hooks:     hooksOut(start.Spec.LifecycleHooks),
		},
		Environment: env,
		Volumes:     volumes,
		Secrets:     secretValues,
		Workspace:   start.WorkspaceName,
	}}}, nil
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

func stopMessage(id string, container execution.ContainerID) *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: id, Body: &hostproto.ServerMessage_Stop{Stop: &hostproto.StopContainer{
		ContainerId: container.String(), GraceSeconds: stopGraceSeconds,
	}}}
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
	if e.GetExitCode() != 0 {
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
	case hostproto.ExitReason_EXIT_REASON_CRASHED, hostproto.ExitReason_EXIT_REASON_UNSPECIFIED:
		exit.Reason = execution.StopCrashed
	}
	return exit
}
