// Package hostsession serves the host connection: enrollment, the control
// session that carries commands and container reports, and the task calls
// agents make for their containers. It authenticates hosts and maps the
// protocol to compute and execution; those owners make every decision.
package hostsession

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/observability"
	"github.com/AmbientWare/lazycloud/internal/secrets"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const (
	// maxRecvBytes admits a 16 MiB result with generous envelope room.
	maxRecvBytes = 64 << 20
	// maxSendBytes admits a claim of execution.MaxClaimInputBytes of inputs
	// with generous envelope room, well under the agent's receive limit.
	maxSendBytes  = execution.MaxClaimInputBytes + 16<<20
	maxClaimWait  = 60 * time.Second
	maxClaimTasks = 256
)

// Config tunes the host connection.
type Config struct {
	// ImageTemplate is the container image for a Python version; {version}
	// is replaced with the release's python_version.
	ImageTemplate string
	// TouchInterval is how often a session records presence and checks that
	// it is still the host's latest session.
	TouchInterval time.Duration
	// Secrets resolves the secrets each start carries.
	Secrets *secrets.Secrets
	// ContainerAPI serves container API requests; see api.NewContainerHandler.
	ContainerAPI http.Handler
	// Observability stores container metrics and start stages; nil drops
	// them.
	Observability *observability.Observability
	// SSH holds the keys of pods that serve SSH.
	SSH *execution.SSHKeys
}

// Server implements hostproto.HostService.
type Server struct {
	hostproto.UnimplementedHostServiceServer

	compute   *compute.Compute
	execution *execution.Execution
	storage   *storage.Storage
	images    *images.Images
	listener  *database.Listener
	config    Config
	logger    *slog.Logger

	// lifetime ends sessions and long polls when the server shuts down.
	lifetime context.Context //nolint:containedctx // The server's own lifetime, cancelled by Shutdown.
	shutdown context.CancelFunc
	// receivers are the per-session goroutines reading host messages. Each
	// ends when its RPC ends.
	receivers sync.WaitGroup
}

// NewServer returns the host service. listener must listen on
// database.ChannelHost and database.ChannelClaim.
func NewServer(c *compute.Compute, e *execution.Execution, s *storage.Storage, im *images.Images, listener *database.Listener, config Config, logger *slog.Logger) *Server {
	lifetime, shutdown := context.WithCancel(context.Background())
	return &Server{
		compute: c, execution: e, storage: s, images: im, listener: listener, config: config, logger: logger,
		lifetime: lifetime, shutdown: shutdown,
	}
}

// ServerOptions are the gRPC options the service needs: host authentication,
// message limits and keepalive.
func (s *Server) ServerOptions() []grpc.ServerOption {
	return []grpc.ServerOption{
		grpc.UnaryInterceptor(s.authenticateUnary),
		grpc.StreamInterceptor(s.authenticateStream),
		grpc.MaxRecvMsgSize(maxRecvBytes),
		grpc.MaxSendMsgSize(maxSendBytes),
		grpc.KeepaliveEnforcementPolicy(keepalive.EnforcementPolicy{MinTime: 10 * time.Second, PermitWithoutStream: true}),
		grpc.KeepaliveParams(keepalive.ServerParameters{Time: 30 * time.Second, Timeout: 10 * time.Second}),
	}
}

// Shutdown ends open sessions and long polls so a graceful stop completes.
func (s *Server) Shutdown() { s.shutdown() }

// Wait returns once every session's receive goroutine has ended. Call it
// after the gRPC server stopped.
func (s *Server) Wait() { s.receivers.Wait() }

type hostKey struct{}

func hostFrom(ctx context.Context) compute.HostID {
	host, _ := ctx.Value(hostKey{}).(compute.HostID)
	return host
}

// HostFrom is the host that authenticated a call on the server's connection,
// for other services registered with ServerOptions.
func HostFrom(ctx context.Context) (compute.HostID, bool) {
	host, ok := ctx.Value(hostKey{}).(compute.HostID)
	return host, ok
}

func (s *Server) authenticate(ctx context.Context) (context.Context, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	values := md.Get("authorization")
	if len(values) != 1 {
		return nil, status.Error(codes.Unauthenticated, "a host token is required")
	}
	scheme, token, ok := strings.Cut(values[0], " ")
	if !ok || !strings.EqualFold(scheme, "bearer") {
		return nil, status.Error(codes.Unauthenticated, "a host token is required")
	}
	host, err := s.compute.AuthenticateHost(ctx, token)
	if err != nil {
		return nil, s.grpcError(ctx, err)
	}
	// Logs of the host's calls carry its id.
	ctx = telemetry.With(ctx, slog.String(telemetry.KeyHost, host.String()))
	return context.WithValue(ctx, hostKey{}, host), nil
}

func (s *Server) authenticateUnary(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
	if info.FullMethod == hostproto.HostService_Enroll_FullMethodName {
		return handler(ctx, req)
	}
	ctx, err := s.authenticate(ctx)
	if err != nil {
		return nil, err
	}
	return handler(ctx, req)
}

type authenticatedStream struct {
	grpc.ServerStream
	ctx context.Context //nolint:containedctx // Carries the host for the stream's lifetime.
}

func (a authenticatedStream) Context() context.Context { return a.ctx }

func (s *Server) authenticateStream(srv any, ss grpc.ServerStream, _ *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
	ctx, err := s.authenticate(ss.Context())
	if err != nil {
		return err
	}
	return handler(srv, authenticatedStream{ServerStream: ss, ctx: ctx})
}

// grpcError maps owner errors to status codes. Unexpected errors are logged
// and reported without detail.
func (s *Server) grpcError(ctx context.Context, err error) error {
	var refused *compute.IdentityError
	switch {
	case errors.Is(err, compute.ErrUnknownHost):
		return status.Error(codes.Unauthenticated, "unknown host")
	case errors.Is(err, compute.ErrInvalidJoinToken):
		return status.Error(codes.PermissionDenied, "the join token is invalid, used or expired")
	case errors.As(err, &refused):
		return status.Error(codes.PermissionDenied, refused.Error())
	case errors.Is(err, execution.ErrStaleAttempt):
		return status.Error(codes.FailedPrecondition, "the attempt is no longer running on this container")
	case errors.Is(err, execution.ErrNotAssigned):
		return status.Error(codes.PermissionDenied, "the container is not assigned to this host")
	case errors.Is(err, images.ErrStaleBuild):
		return status.Error(codes.FailedPrecondition, "the build already finished")
	case errors.Is(err, context.Canceled):
		return status.Error(codes.Canceled, "cancelled")
	case errors.Is(err, context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, "deadline exceeded")
	}
	s.logger.ErrorContext(ctx, "host call failed", "host", hostFrom(ctx).String(), "error", err)
	return status.Error(codes.Internal, "internal error")
}

// withLifetime ends ctx when the server shuts down.
func (s *Server) withLifetime(ctx context.Context) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(ctx)
	stop := context.AfterFunc(s.lifetime, cancel)
	return ctx, func() { stop(); cancel() }
}

// Enroll exchanges a join token or a cloud identity proof for a host
// identity.
func (s *Server) Enroll(ctx context.Context, req *hostproto.EnrollRequest) (*hostproto.EnrollResponse, error) {
	if req.GetHostname() == "" || req.GetCapacity().GetCpuMillis() <= 0 || req.GetCapacity().GetMemoryBytes() <= 0 {
		return nil, status.Error(codes.InvalidArgument, "hostname and positive capacity are required")
	}
	host, token, err := s.enroll(ctx, req)
	if err != nil {
		return nil, s.grpcError(ctx, err)
	}
	return &hostproto.EnrollResponse{HostId: host.String(), HostToken: token}, nil
}

func parseContainer(id string) (execution.ContainerID, error) {
	parsed, err := uuid.Parse(id)
	if err != nil {
		return execution.ContainerID{}, status.Error(codes.InvalidArgument, "container_id is not a UUID")
	}
	return execution.ContainerID(parsed), nil
}

// ClaimTasks waits for queued tasks the container can run.
func (s *Server) ClaimTasks(ctx context.Context, req *hostproto.ClaimTasksRequest) (*hostproto.ClaimTasksResponse, error) {
	container, err := parseContainer(req.GetContainerId())
	if err != nil {
		return nil, err
	}
	maxTasks := min(max(int(req.GetMaxTasks()), 1), maxClaimTasks)
	wait := min(max(time.Duration(req.GetWaitSeconds())*time.Second, 0), maxClaimWait)
	ctx, cancel := s.withLifetime(ctx)
	defer cancel()
	claimed, err := s.execution.ClaimTasks(ctx, s.listener, hostFrom(ctx), container, maxTasks, wait)
	if err != nil {
		return nil, s.grpcError(ctx, err)
	}
	if s.config.Observability != nil && len(claimed) > 0 {
		started := make([]execution.TaskID, len(claimed))
		for n, c := range claimed {
			started[n] = c.Task
		}
		s.config.Observability.TasksStarted(started)
	}
	out := &hostproto.ClaimTasksResponse{Tasks: make([]*hostproto.ClaimedTask, len(claimed))}
	for n, c := range claimed {
		out.Tasks[n] = &hostproto.ClaimedTask{
			TaskId:        c.Task.String(),
			AttemptId:     c.Attempt.String(),
			AttemptNumber: int32(c.Number), //nolint:gosec // Attempts are capped at 100.
			InputEncoding: encodingOut(c.Input.Encoding),
			Input:         c.Input.Data,
			Deadline:      timestamppb.New(c.Deadline),
			RootTaskId:    c.Root.String(),
			MaxAttempts:   int32(c.MaxAttempts), //nolint:gosec // Attempts are capped at 100.
			Traceparent:   c.TraceParent,
		}
		if c.Parent != nil {
			out.Tasks[n].ParentTaskId = c.Parent.String()
		}
		for _, dep := range c.Dependencies {
			out.Tasks[n].Dependencies = append(out.Tasks[n].Dependencies, &hostproto.DependencyResult{
				TaskId: dep.Task.String(), Encoding: encodingOut(dep.Result.Encoding), Data: dep.Result.Data,
			})
		}
	}
	return out, nil
}

// CompleteTask records an attempt's outcome.
func (s *Server) CompleteTask(ctx context.Context, req *hostproto.CompleteTaskRequest) (*hostproto.CompleteTaskResponse, error) {
	container, err := parseContainer(req.GetContainerId())
	if err != nil {
		return nil, err
	}
	attempt, err := uuid.Parse(req.GetAttemptId())
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, "attempt_id is not a UUID")
	}
	ctx = telemetry.With(ctx, slog.String(telemetry.KeyContainer, container.String()), slog.String(telemetry.KeyAttempt, attempt.String()))
	outcome := execution.AttemptOutcome{Attempt: execution.AttemptID(attempt)}
	switch o := req.GetOutcome().(type) {
	case *hostproto.CompleteTaskRequest_Success:
		encoding, ok := encodingIn(o.Success.GetEncoding())
		if !ok {
			return nil, status.Error(codes.InvalidArgument, "success needs an encoding")
		}
		outcome.State = execution.AttemptSucceeded
		outcome.Result = &execution.Payload{Encoding: encoding, Data: o.Success.GetResult()}
	case *hostproto.CompleteTaskRequest_Failure:
		failure, ok := failureIn(o.Failure)
		if !ok {
			return nil, status.Error(codes.InvalidArgument, "failure needs a kind")
		}
		outcome.State = execution.AttemptFailed
		outcome.Failure = &failure
	default:
		return nil, status.Error(codes.InvalidArgument, "an outcome is required")
	}
	if err := s.execution.CompleteAttempt(ctx, hostFrom(ctx), container, outcome); err != nil {
		return nil, s.grpcError(ctx, err)
	}
	return &hostproto.CompleteTaskResponse{}, nil
}

// AppendLogs stores output. An attempt's lines go to its task's log; lines
// outside attempts, such as import output and HTTP requests, go to the
// container's log.
func (s *Server) AppendLogs(ctx context.Context, req *hostproto.AppendLogsRequest) (*hostproto.AppendLogsResponse, error) {
	container, err := parseContainer(req.GetContainerId())
	if err != nil {
		return nil, err
	}
	lines := make([]execution.LogLine, 0, len(req.GetLines()))
	var containerLines []execution.ContainerLogLine
	for _, line := range req.GetLines() {
		stream, ok := streamIn(line.GetStream())
		if !ok {
			return nil, status.Error(codes.InvalidArgument, "a log line needs a stream")
		}
		at := time.Now()
		if line.GetTime() != nil {
			at = line.GetTime().AsTime()
		}
		if line.GetAttemptId() == "" {
			containerLine := execution.ContainerLogLine{LogLine: execution.LogLine{Stream: stream, Data: line.GetData(), Time: at}}
			if id := line.GetRequestId(); id != "" {
				request, err := uuid.Parse(id)
				if err != nil {
					return nil, status.Error(codes.InvalidArgument, "request_id is not a UUID")
				}
				containerLine.Request = &request
			}
			containerLines = append(containerLines, containerLine)
			continue
		}
		attempt, err := uuid.Parse(line.GetAttemptId())
		if err != nil {
			return nil, status.Error(codes.InvalidArgument, "attempt_id is not a UUID")
		}
		lines = append(lines, execution.LogLine{Attempt: execution.AttemptID(attempt), Stream: stream, Data: line.GetData(), Time: at})
	}
	if err := s.execution.AppendLogs(ctx, hostFrom(ctx), container, lines); err != nil {
		return nil, s.grpcError(ctx, err)
	}
	if err := s.execution.AppendContainerLogs(ctx, hostFrom(ctx), container, containerLines); err != nil {
		return nil, s.grpcError(ctx, err)
	}
	return &hostproto.AppendLogsResponse{}, nil
}

func encodingIn(e hostproto.PayloadEncoding) (execution.Encoding, bool) {
	switch e {
	case hostproto.PayloadEncoding_PAYLOAD_ENCODING_JSON:
		return execution.EncodingJSON, true
	case hostproto.PayloadEncoding_PAYLOAD_ENCODING_CLOUDPICKLE:
		return execution.EncodingCloudpickle, true
	case hostproto.PayloadEncoding_PAYLOAD_ENCODING_UNSPECIFIED:
	}
	return "", false
}

func encodingOut(e execution.Encoding) hostproto.PayloadEncoding {
	switch e {
	case execution.EncodingJSON:
		return hostproto.PayloadEncoding_PAYLOAD_ENCODING_JSON
	case execution.EncodingCloudpickle:
		return hostproto.PayloadEncoding_PAYLOAD_ENCODING_CLOUDPICKLE
	}
	return hostproto.PayloadEncoding_PAYLOAD_ENCODING_UNSPECIFIED
}

func streamIn(s hostproto.LogStream) (execution.LogStream, bool) {
	switch s {
	case hostproto.LogStream_LOG_STREAM_STDOUT:
		return execution.LogStdout, true
	case hostproto.LogStream_LOG_STREAM_STDERR:
		return execution.LogStderr, true
	case hostproto.LogStream_LOG_STREAM_SYSTEM:
		return execution.LogSystem, true
	case hostproto.LogStream_LOG_STREAM_UNSPECIFIED:
	}
	return "", false
}

func failureIn(f *hostproto.TaskFailure) (execution.Failure, bool) {
	switch f.GetKind() {
	case hostproto.AttemptFailureKind_ATTEMPT_FAILURE_KIND_USER_ERROR:
		return execution.Failure{
			Kind: execution.FailureUserError, Type: f.GetError().GetType(), Message: f.GetError().GetMessage(),
			Traceback: f.GetError().GetTraceback(), Exception: f.GetException(),
		}, true
	case hostproto.AttemptFailureKind_ATTEMPT_FAILURE_KIND_CRASHED:
		return execution.CrashFailure(f.GetError().GetMessage()), true
	case hostproto.AttemptFailureKind_ATTEMPT_FAILURE_KIND_UNSPECIFIED:
	}
	return execution.Failure{}, false
}
