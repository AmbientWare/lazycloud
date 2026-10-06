package edge

import (
	"context"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/execution"
)

const (
	// publishInterval paces demand publication.
	publishInterval = time.Second
	// renewAfter republishes unchanged demand well before its lease lapses.
	renewAfter = execution.EndpointLoadTTL / 3
	// failureCheckInterval bounds how often waiting requests of one release
	// ask whether it can start.
	failureCheckInterval = time.Second
)

// releaseLoad is this edge's demand on one release. Requests count against
// the release they target, so the active release of a deploy scales up
// while an older one still serves them.
type releaseLoad struct {
	release  uuid.UUID
	keepWarm time.Duration
	inFlight int
	waiting  int
	window   windowMax
	served   int64
	// wake asks the next publication to wake planning.
	wake bool
	// published is what the planner last saw from this edge.
	published  execution.EndpointLoad
	renewedAt  time.Time
	checkedAt  time.Time
	everPushed bool
}

// overPending reports whether the release already has max_pending requests
// waiting at this edge.
func (e *Edge) overPending(r *release) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	l := e.loads[r.id]
	return l != nil && l.waiting >= r.maxPending
}

// demandLocked returns the release's load, creating it. Call with e.mu held.
func (e *Edge) demandLocked(r *release) *releaseLoad {
	l := e.loads[r.id]
	if l == nil {
		l = &releaseLoad{release: r.id, keepWarm: r.keepWarm}
		e.loads[r.id] = l
	}
	return l
}

// sample records the current demand in the keep-warm window. Call with
// e.mu held.
func (l *releaseLoad) sample(now time.Time) {
	l.window.push(now, l.inFlight+l.waiting, l.keepWarm)
}

// failureCheckIn is how long until a waiting request of the release may
// ask whether it can start; zero means now, and the ask is recorded.
func (l *releaseLoad) failureCheckIn(now time.Time) time.Duration {
	if wait := failureCheckInterval - now.Sub(l.checkedAt); wait > 0 {
		return wait
	}
	l.checkedAt = now
	return 0
}

// kick publishes a release's demand at once and wakes planning, for a
// request that found no container.
func (e *Edge) kick(release uuid.UUID) {
	e.mu.Lock()
	if l := e.loads[release]; l != nil {
		l.wake = true
	}
	e.mu.Unlock()
	select {
	case e.publish <- struct{}{}:
	default:
	}
}

// publishLoads upserts changed demand every publishInterval, or at once
// after a kick, until ctx ends.
func (e *Edge) publishLoads(ctx context.Context) {
	ticker := time.NewTicker(publishInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		case <-e.publish:
		}
		loads, wake := e.collectLoads(time.Now())
		if len(loads) == 0 {
			continue
		}
		if err := e.execution.PublishEndpointLoads(ctx, e.id, loads, wake); err != nil {
			if ctx.Err() != nil {
				return
			}
			e.logger.WarnContext(ctx, "publish endpoint demand", "error", err)
			// Publish everything again next time.
			e.mu.Lock()
			for _, l := range e.loads {
				l.renewedAt = time.Time{}
				l.wake = l.wake || l.waiting > 0
			}
			e.mu.Unlock()
		}
	}
}

// collectLoads returns the demand to publish: what changed, what needs
// renewing, and a final zero for releases whose demand ended, which are then
// forgotten.
func (e *Edge) collectLoads(now time.Time) ([]execution.EndpointLoad, []uuid.UUID) {
	e.mu.Lock()
	defer e.mu.Unlock()
	var loads []execution.EndpointLoad
	var wake []uuid.UUID
	for id, l := range e.loads {
		current := execution.EndpointLoad{
			Release: id, InFlight: l.inFlight, Waiting: l.waiting,
			WindowPeak: l.window.max(now, l.inFlight+l.waiting, l.keepWarm),
		}
		idle := current.InFlight == 0 && current.Waiting == 0 && current.WindowPeak == 0
		if idle && (!l.everPushed || l.published == current) {
			delete(e.loads, id)
			continue
		}
		if current == l.published && now.Sub(l.renewedAt) < renewAfter && !l.wake {
			continue
		}
		loads = append(loads, current)
		if l.wake && current.Waiting > 0 {
			wake = append(wake, id)
		}
		l.wake = false
		l.published, l.renewedAt, l.everPushed = current, now, true
	}
	return loads, wake
}

// windowMax is the largest demand seen within a release's keep-warm
// window: a deque of samples in time order with falling values.
type windowMax struct {
	at     []time.Time
	values []int
}

func (w *windowMax) push(now time.Time, value int, window time.Duration) {
	w.expire(now, window)
	for n := len(w.values); n > 0 && w.values[n-1] <= value; n = len(w.values) {
		w.at, w.values = w.at[:n-1], w.values[:n-1]
	}
	w.at, w.values = append(w.at, now), append(w.values, value)
}

// max is the window's peak, at least current.
func (w *windowMax) max(now time.Time, current int, window time.Duration) int {
	w.expire(now, window)
	if len(w.values) > 0 && w.values[0] > current {
		return w.values[0]
	}
	return current
}

func (w *windowMax) expire(now time.Time, window time.Duration) {
	cut := 0
	// Keep the newest sample older than the window start: its value held
	// until the next sample, which may be inside the window.
	for cut+1 < len(w.at) && now.Sub(w.at[cut+1]) > window {
		cut++
	}
	if cut > 0 {
		w.at, w.values = w.at[cut:], w.values[cut:]
	}
	if len(w.at) == 1 && now.Sub(w.at[0]) > window {
		w.at, w.values = w.at[:0], w.values[:0]
	}
}
