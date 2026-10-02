package observability

import (
	"context"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/prometheus/client_golang/prometheus"

	"github.com/AmbientWare/lazycloud/internal/execution"
)

const (
	// startedWindow is how long started tasks gather before one
	// notification publishes them.
	startedWindow = 100 * time.Millisecond
	// maxStarted bounds the tasks waiting to be published.
	maxStarted = 100000
)

// startedQueue holds tasks claims started, published together off the
// claim path. A claim that notified in its own transaction would take
// PostgreSQL's global notification lock through its commit and serialize
// every claim; one notification per window takes it once.
type startedQueue struct {
	mu      sync.Mutex
	ids     map[uuid.UUID]struct{}
	kick    chan struct{}
	dropped prometheus.Counter
}

func newStartedQueue(registerer prometheus.Registerer) *startedQueue {
	q := &startedQueue{
		ids:  map[uuid.UUID]struct{}{},
		kick: make(chan struct{}, 1),
		dropped: prometheus.NewCounter(prometheus.CounterOpts{
			Name: "lazycloud_started_changes_dropped_total",
			Help: "Started tasks left out of the change stream because too many waited.",
		}),
	}
	if registerer != nil {
		registerer.MustRegister(q.dropped)
	}
	return q
}

// TasksStarted queues the tasks a claim committed as running for the
// change stream, without waiting.
func (o *Observability) TasksStarted(tasks []execution.TaskID) {
	q := o.started
	q.mu.Lock()
	for _, t := range tasks {
		if len(q.ids) >= maxStarted {
			q.dropped.Inc()
			continue
		}
		q.ids[uuid.UUID(t)] = struct{}{}
	}
	q.mu.Unlock()
	select {
	case q.kick <- struct{}{}:
	default:
	}
}

// RunStartedPublisher publishes queued started tasks with their current
// status, at most once per window, until ctx ends. It does nothing while no
// task waits. A task that finished meanwhile is published as finished,
// which its own transition also sent.
func (o *Observability) RunStartedPublisher(ctx context.Context) error {
	q := o.started
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-q.kick:
		}
		timer := time.NewTimer(startedWindow)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil
		case <-timer.C:
		}
		q.mu.Lock()
		ids := make([]uuid.UUID, 0, len(q.ids))
		for id := range q.ids {
			ids = append(ids, id)
		}
		clear(q.ids)
		q.mu.Unlock()
		if len(ids) == 0 {
			continue
		}
		if err := o.queries.PublishTaskStates(ctx, ids); err != nil {
			o.logger.WarnContext(ctx, "publishing started tasks failed", "tasks", len(ids), "error", err)
		}
	}
}
