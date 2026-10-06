package agent

import (
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
	"slices"
	"strings"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	// maxContainerAPICalls bounds container API calls in flight per
	// container. The supervisor queues callers below it; a container that
	// bypasses the supervisor is refused above it.
	maxContainerAPICalls = 64
	// maxContainerAPIBody bounds one request body, which the server also
	// enforces.
	maxContainerAPIBody = 32 << 20
)

// containerRuntime is how a container's supervisor runs its slots.
type containerRuntime struct {
	InProcess bool                      `json:"in_process,omitempty"`
	Hooks     *hostproto.LifecycleHooks `json:"hooks,omitempty"`
	// SecretEnv names the environment variables holding secret values.
	SecretEnv []string `json:"secret_env,omitempty"`
}

func runtimeOf(spec *hostproto.StartContainer) containerRuntime {
	return containerRuntime{
		InProcess: spec.GetFunction().GetInProcess(),
		Hooks:     spec.GetFunction().GetHooks(),
		SecretEnv: slices.Sorted(maps.Keys(spec.GetSecrets())),
	}
}

// API relays one container API call to the server on the payload
// connection. The supervisor is untrusted, so the agent names the container
// itself and bounds the body and the calls in flight.
func (l *link) API(stream hostproto.ContainerLink_APIServer) error {
	select {
	case l.c.apiCalls <- struct{}{}:
		defer func() { <-l.c.apiCalls }()
	default:
		return status.Errorf(codes.ResourceExhausted, "more than %d container API calls are in flight", maxContainerAPICalls)
	}
	first, err := stream.Recv()
	if err != nil {
		return fmt.Errorf("receive request head: %w", err)
	}
	if first.GetHead() == nil {
		return status.Error(codes.InvalidArgument, "the first message must carry the request head")
	}
	first.Head.ContainerId = l.c.id
	if len(first.GetBody()) > maxContainerAPIBody {
		return status.Error(codes.ResourceExhausted, "the request body is too large")
	}
	ctx, cancel := context.WithCancel(l.c.taskContext(stream.Context(), taskOf(first.GetHead())))
	defer cancel()
	up, err := l.c.a.host.ContainerAPI(ctx)
	if err != nil {
		return fmt.Errorf("open container API call: %w", err)
	}
	if err := up.Send(first); err != nil {
		return fmt.Errorf("forward request head: %w", err)
	}
	// The body copy ends when the supervisor half-closes, or when this
	// handler returns and the stream ends; the link waits for it on close.
	l.goroutines.Go(func() {
		total := len(first.GetBody())
		for {
			msg, err := stream.Recv()
			if err != nil {
				if errors.Is(err, io.EOF) {
					_ = up.CloseSend()
				}
				return
			}
			total += len(msg.GetBody())
			if msg.GetHead() != nil || total > maxContainerAPIBody {
				cancel()
				return
			}
			if up.Send(msg) != nil {
				return
			}
		}
	})
	for {
		msg, err := up.Recv()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			// Keep the server's status, such as a stale task.
			return err //nolint:wrapcheck // The status passes through to the supervisor.
		}
		if err := stream.Send(msg); err != nil {
			return fmt.Errorf("forward response: %w", err)
		}
	}
}

// taskHeader names the task whose attempt makes a container API call, as
// the server reads it.
const taskHeader = "LazyCloud-Task"

// taskOf is the task head names, or "".
func taskOf(head *hostproto.APIRequestHead) string {
	for _, h := range head.GetHeaders() {
		if strings.EqualFold(h.GetName(), taskHeader) {
			return h.GetValue()
		}
	}
	return ""
}
