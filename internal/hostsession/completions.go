package hostsession

import (
	"context"
	"fmt"
	"sync"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/execution"
)

const (
	// completionBatch bounds the completions one transaction writes.
	completionBatch = 200
	// completionBacklog bounds the completions of one host waiting for a
	// write. Past it a call fails with RESOURCE_EXHAUSTED and the agent
	// retries it.
	completionBacklog = 2000
	// completionWriteTimeout bounds one batch's transaction. The agent
	// waits no longer for a CompleteTask call.
	completionWriteTimeout = 15 * time.Second
)

// completions are the hosts' queues of completions waiting to be written.
// A host has a queue while its writer runs. The writer takes everything
// queued, up to completionBatch, writes it in one transaction and repeats
// until the queue is empty. Completions that arrive during a write share
// the next one, and a completion that arrives alone is written at once.
type completions struct {
	mu     sync.Mutex
	queues map[compute.HostID][]*pendingCompletion
	// writers are the hosts' writer goroutines. Each ends once its queue
	// is empty.
	writers sync.WaitGroup
}

type pendingCompletion struct {
	completion execution.Completion
	done       chan error
	// batch is how many completions its write wrote, set before done.
	batch int
}

// complete queues c behind host's other completions and waits for its write
// or for ctx to end. A completion whose caller stopped waiting is still
// written.
func (s *Server) complete(ctx context.Context, host compute.HostID, c execution.Completion) error {
	p := &pendingCompletion{completion: c, done: make(chan error, 1)}
	w := &s.completions
	w.mu.Lock()
	queue, writing := w.queues[host]
	if len(queue) >= completionBacklog {
		w.mu.Unlock()
		return status.Error(codes.ResourceExhausted, "the host has too many completions waiting to be written")
	}
	w.queues[host] = append(queue, p)
	w.mu.Unlock()
	if !writing {
		w.writers.Go(func() { s.writeCompletions(host) }) //nolint:contextcheck // The writer outlives the call that starts it.
	}
	select {
	case err := <-p.done:
		trace.SpanFromContext(ctx).SetAttributes(attribute.Int("lazycloud.completion_batch", p.batch))
		return err
	case <-ctx.Done():
		return fmt.Errorf("wait for the completion write: %w", ctx.Err())
	}
}

// writeCompletions writes host's queued completions until none are left.
// It outlives the calls that queued them, so each write has its own
// deadline rather than a caller's context.
func (s *Server) writeCompletions(host compute.HostID) {
	w := &s.completions
	for {
		w.mu.Lock()
		queue := w.queues[host]
		if len(queue) == 0 {
			delete(w.queues, host)
			w.mu.Unlock()
			return
		}
		batch := queue[:min(len(queue), completionBatch)]
		w.queues[host] = queue[len(batch):]
		w.mu.Unlock()

		in := make([]execution.Completion, len(batch))
		for n, p := range batch {
			in[n] = p.completion
		}
		ctx, cancel := context.WithTimeout(context.Background(), completionWriteTimeout)
		errs := s.execution.CompleteAttempts(ctx, host, in)
		cancel()
		for n, p := range batch {
			p.batch = len(batch)
			p.done <- errs[n]
		}
	}
}
