package edge

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
)

// workloadState is the edge's view of a workload's ready containers and the
// requests each runs. It is loaded on the first request and reloaded when a
// container of the workload changes state.
type workloadState struct {
	id         uuid.UUID
	loaded     bool
	containers map[uuid.UUID]*slot
	// changed is closed and replaced whenever a container appears, goes or
	// frees capacity, waking waiting requests.
	changed chan struct{}
}

// slot is one ready container.
type slot struct {
	id       uuid.UUID
	release  uuid.UUID
	version  int
	host     uuid.UUID
	inflight int
	// avoid is set after the container answered busy or not running, until
	// the next reload of the set says otherwise.
	avoid time.Time
}

// avoidFor keeps the edge off a container that refused a request, so other
// containers or a reload get a chance first.
const avoidFor = 200 * time.Millisecond

func (ws *workloadState) wake() {
	close(ws.changed)
	ws.changed = make(chan struct{})
}

// state returns the workload's state, creating it unloaded. Call with e.mu
// held.
func (e *Edge) stateLocked(workload uuid.UUID) *workloadState {
	ws := e.workloads[workload]
	if ws == nil {
		ws = &workloadState{id: workload, containers: map[uuid.UUID]*slot{}, changed: make(chan struct{})}
		e.workloads[workload] = ws
	}
	return ws
}

// load reads the workload's ready containers into its state, keeping the
// counts of containers still present. Each container's release is cached
// first, since its settings bound the container's capacity.
func (e *Edge) load(ctx context.Context, workload uuid.UUID) error {
	rows, err := e.execution.EndpointContainers(ctx, workload)
	if err != nil {
		return err
	}
	for _, row := range rows {
		if _, err := e.release(ctx, row.Release); err != nil {
			return err
		}
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	ws := e.stateLocked(workload)
	next := make(map[uuid.UUID]*slot, len(rows))
	for _, row := range rows {
		id := uuid.UUID(row.Container)
		if old := ws.containers[id]; old != nil {
			old.avoid = time.Time{}
			next[id] = old
			continue
		}
		next[id] = &slot{id: id, release: row.Release, version: row.Version, host: row.Host}
	}
	ws.containers = next
	ws.loaded = true
	ws.wake()
	return nil
}

// markChanged queues a reload of a tracked workload's containers.
func (e *Edge) markChanged(workload uuid.UUID) {
	e.mu.Lock()
	_, tracked := e.workloads[workload]
	e.mu.Unlock()
	if !tracked {
		return
	}
	select {
	case e.refresh <- workload:
	default:
		// The queue is full; waiting requests reload on their next wake.
	}
}

// refreshAll queues every tracked workload, after the listener reconnects.
func (e *Edge) refreshAll() {
	e.mu.Lock()
	ids := make([]uuid.UUID, 0, len(e.workloads))
	for id := range e.workloads {
		ids = append(ids, id)
	}
	e.mu.Unlock()
	for _, id := range ids {
		e.markChanged(id)
	}
}

// refreshContainers reloads queued workloads one at a time until ctx ends.
func (e *Edge) refreshContainers(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		case id := <-e.refresh:
			if err := e.load(ctx, id); err != nil && ctx.Err() == nil {
				e.logger.WarnContext(ctx, "reload endpoint containers", "workload_id", id, "error", err)
			}
		}
	}
}

// Errors a request can end with before reaching a container.
var (
	errNoCapacity  = errors.New("no container became ready in time")
	errTooManyWait = errors.New("too many requests are waiting for a container")
)

// releaseFailedError means the release's containers cannot start.
type releaseFailedError struct{ reason string }

func (e *releaseFailedError) Error() string { return e.reason }

// lease is one request's hold on a container's capacity.
type lease struct {
	e     *Edge
	ws    *workloadState
	slot  *slot
	load  *releaseLoad
	ended bool
}

// acquire waits until a container of t can take the request and holds one
// unit of its capacity. The request counts as waiting until then, which the
// edge publishes as demand, and as in flight after. It gives up at deadline,
// when t's release cannot start, or with errTooManyWait past max_pending.
func (e *Edge) acquire(ctx context.Context, t target, deadline time.Time) (*lease, error) {
	e.mu.Lock()
	ws := e.stateLocked(t.workload.id)
	loaded := ws.loaded
	e.mu.Unlock()
	if !loaded {
		if err := e.load(ctx, t.workload.id); err != nil {
			return nil, err
		}
	}
	load := e.demand(t.release)
	waiting := false
	defer func() {
		if waiting {
			e.mu.Lock()
			load.waiting--
			load.sample(time.Now())
			e.mu.Unlock()
		}
	}()
	timer := time.NewTimer(time.Until(deadline))
	defer timer.Stop()
	for {
		e.mu.Lock()
		s, any := e.pickLocked(ws, t)
		if s != nil {
			s.inflight++
			if waiting {
				load.waiting--
				waiting = false
			}
			load.inFlight++
			load.sample(time.Now())
			e.mu.Unlock()
			return &lease{e: e, ws: ws, slot: s, load: load}, nil
		}
		if !waiting {
			if load.waiting >= t.release.maxPending {
				e.mu.Unlock()
				return nil, errTooManyWait
			}
			load.waiting++
			waiting = true
			load.sample(time.Now())
		}
		cold := !any
		check := cold && load.dueFailureCheck(time.Now())
		changed := ws.changed
		e.mu.Unlock()
		if cold {
			// No container of the release runs: publish now so planning
			// starts one, and fail fast when the release cannot start.
			e.kick(t.release.id)
		}
		if check {
			if err := e.checkFailure(ctx, t.release.id); err != nil {
				return nil, err
			}
		}
		select {
		case <-changed:
		case <-timer.C:
			return nil, errNoCapacity
		case <-ctx.Done():
			return nil, fmt.Errorf("wait for a container: %w", ctx.Err())
		}
	}
}

// pickLocked chooses the container with the most free capacity among those
// that may serve t, and reports whether t has any container at all.
func (e *Edge) pickLocked(ws *workloadState, t target) (*slot, bool) {
	now := time.Now()
	candidates := func(match func(*slot) bool) (*slot, bool) {
		var best *slot
		var bestFree int
		found := false
		for _, s := range ws.containers {
			if !match(s) {
				continue
			}
			found = true
			if now.Before(s.avoid) {
				continue
			}
			free := e.capacityLocked(s) - s.inflight
			if free > 0 && (best == nil || free > bestFree) {
				best, bestFree = s, free
			}
		}
		return best, found
	}
	if t.container != nil {
		return candidates(func(s *slot) bool { return s.id == *t.container })
	}
	if best, found := candidates(func(s *slot) bool { return s.release == t.release.id }); found || !t.latest {
		return best, found
	}
	// The active release has no ready container yet: the newest deployed
	// release that has one keeps serving until it does.
	newest := 0
	for _, s := range ws.containers {
		if s.version > newest {
			newest = s.version
		}
	}
	if newest == 0 {
		return nil, false
	}
	return candidates(func(s *slot) bool { return s.version == newest })
}

// capacityLocked is the requests a container admits, from its own
// release's settings.
func (e *Edge) capacityLocked(s *slot) int {
	if r := e.releases[s.release]; r != nil {
		return r.capacity
	}
	return 1
}

// end releases the lease's capacity and wakes waiting requests.
func (l *lease) end() {
	e := l.e
	e.mu.Lock()
	defer e.mu.Unlock()
	if l.ended {
		return
	}
	l.ended = true
	l.slot.inflight--
	l.load.inFlight--
	l.load.sample(time.Now())
	l.load.served++
	l.ws.wake()
}

// refused ends the lease after the container refused the request, and
// keeps other requests off it briefly.
func (l *lease) refused(reload bool) {
	l.e.mu.Lock()
	l.slot.avoid = time.Now().Add(avoidFor)
	l.e.mu.Unlock()
	l.end()
	if reload {
		l.e.markChanged(l.ws.id)
	}
}

// checkFailure returns a releaseFailedError when the release's containers
// cannot start.
func (e *Edge) checkFailure(ctx context.Context, release uuid.UUID) error {
	failures, err := e.execution.ReleaseFailures(ctx, []uuid.UUID{release})
	if err != nil {
		return err
	}
	f, failed := failures[release]
	if !failed {
		return nil
	}
	if f.LoadError != "" {
		return &releaseFailedError{reason: "the handler failed to load: " + f.LoadError}
	}
	return &releaseFailedError{reason: fmt.Sprintf("the container failed to start %d times", f.StartFailures)}
}
