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

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
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
	epoch, err := s.compute.OpenSession(ctx, host, hello.GetBootId(), compute.Capacity{
		CPUMillis: hello.GetCapacity().GetCpuMillis(), MemoryBytes: hello.GetCapacity().GetMemoryBytes(),
	})
	if err != nil {
		return s.grpcError(ctx, err)
	}
	sess := &session{server: s, stream: stream, host: host, sent: map[string]bool{}}
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
		actions, err := sess.server.execution.ApplyReport(ctx, sess.host, report)
		if err != nil {
			return sess.server.grpcError(ctx, err)
		}
		return sess.sendActions(actions)
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
		if err != nil {
			return sess.server.grpcError(ctx, err)
		}
		if err := sess.send(msg); err != nil {
			return err
		}
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
	sess.sent = derived
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
	return &hostproto.ServerMessage{CommandId: id, Body: &hostproto.ServerMessage_Start{Start: &hostproto.StartContainer{
		ContainerId:   start.Container.String(),
		Image:         strings.ReplaceAll(s.config.ImageTemplate, "{version}", version),
		PythonVersion: version,
		Source:        &hostproto.Source{Sha256: start.Source.String(), Url: url, UrlExpiresAt: timestamppb.New(expires)},
		Resources: &hostproto.Resources{
			CpuMillis: start.CPUMillis, MemoryBytes: start.MemoryBytes,
			CpuLimitMillis: start.CPULimitMillis, MemoryLimitBytes: start.MemoryLimitBytes,
		},
		Function: &hostproto.FunctionWorkload{
			Handler: start.Spec.Handler,
			Slots:   int32(start.Slots), //nolint:gosec // The schema caps concurrency at 256.
		},
		Environment: env,
	}}}, nil
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
