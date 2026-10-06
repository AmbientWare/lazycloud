package snapshotter

import (
	"context"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel/attribute"
	oteltrace "go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/metadata"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// readIdle is how long a start's layers go unread before its read summary
// ends. A start reads in a burst until it is ready.
const readIdle = 2 * time.Second

// startTrace is the trace of one container start the agent granted layers
// for, and what its layers' reads did since.
type startTrace struct {
	name    string
	parent  oteltrace.SpanContext
	granted time.Time

	reads, hits, misses int
	missTime, maxMiss   time.Duration
	missBytes           int64
	first, last         time.Time
}

// startTraces ties the snapshotter's work for a layer to the trace of the
// newest start that granted it: the layer's index fetch and mount, and one
// span summing its frame reads. Containerd's calls carry no trace, and a
// FUSE read names no container, so a layer two starts share counts toward
// the newer one. It holds at most maxTraces starts.
type startTraces struct {
	tracer oteltrace.Tracer
	// active counts starts so reads skip the lock when there are none.
	active atomic.Int32

	mu      sync.Mutex
	byName  map[string]*startTrace
	byLayer map[imagefs.Digest]*startTrace
}

func newStartTraces(tracer oteltrace.Tracer) *startTraces {
	return &startTraces{tracer: tracer, byName: map[string]*startTrace{}, byLayer: map[imagefs.Digest]*startTrace{}}
}

// incomingParent is the trace the agent's call carries, if sampled.
func incomingParent(ctx context.Context) oteltrace.SpanContext {
	md, _ := metadata.FromIncomingContext(ctx)
	values := md.Get("traceparent")
	if len(values) == 0 {
		return oteltrace.SpanContext{}
	}
	return telemetry.SpanContextOf(values[0])
}

// begin records that the start name, traced under parent, uses layers.
func (t *startTraces) begin(name string, parent oteltrace.SpanContext, layers []imagefs.Digest) {
	if name == "" || !parent.IsSampled() {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if old, ok := t.byName[name]; ok {
		t.removeLocked(old)
	}
	if len(t.byName) >= maxTraces {
		var oldest *startTrace
		for _, s := range t.byName {
			if oldest == nil || s.granted.Before(oldest.granted) {
				oldest = s
			}
		}
		t.removeLocked(oldest)
	}
	s := &startTrace{name: name, parent: parent, granted: time.Now()}
	t.byName[name] = s
	for _, l := range layers {
		t.byLayer[l] = s
	}
	t.active.Store(int32(len(t.byName))) //nolint:gosec // at most maxTraces
}

func (t *startTraces) removeLocked(s *startTrace) {
	delete(t.byName, s.name)
	for l, owner := range t.byLayer {
		if owner == s {
			delete(t.byLayer, l)
		}
	}
	t.active.Store(int32(len(t.byName))) //nolint:gosec // at most maxTraces
}

// start starts a span for layer in the trace of the start that granted it,
// or returns ok false when no traced start did.
func (t *startTraces) start(layer imagefs.Digest, name string, attrs ...attribute.KeyValue) (oteltrace.Span, bool) {
	if t.active.Load() == 0 {
		return nil, false
	}
	t.mu.Lock()
	s := t.byLayer[layer]
	t.mu.Unlock()
	if s == nil {
		return nil, false
	}
	ctx := oteltrace.ContextWithRemoteSpanContext(context.Background(), s.parent)
	_, span := t.tracer.Start(ctx, name, oteltrace.WithAttributes(append(attrs, attribute.String(telemetry.AttrLayer, string(layer)))...))
	return span, true
}

// read counts a read of a frame of layer: a cache hit, or a miss that
// waited for a fetch of n bytes.
func (t *startTraces) read(layer imagefs.Digest, hit bool, waited time.Duration, n int) {
	if t.active.Load() == 0 {
		return
	}
	now := time.Now()
	t.mu.Lock()
	defer t.mu.Unlock()
	s := t.byLayer[layer]
	if s == nil {
		return
	}
	if s.reads == 0 {
		s.first = now.Add(-waited)
	}
	s.reads++
	s.last = now
	if hit {
		s.hits++
		return
	}
	s.misses++
	s.missTime += waited
	s.maxMiss = max(s.maxMiss, waited)
	s.missBytes += int64(n)
}

// flush ends the read summaries of starts idle for readIdle, and drops
// starts that read nothing within traceLife.
func (t *startTraces) flush(now time.Time) {
	t.mu.Lock()
	var done []*startTrace
	for _, s := range t.byName {
		if (s.reads > 0 && now.Sub(s.last) >= readIdle) || (s.reads == 0 && now.Sub(s.granted) >= traceLife) {
			done = append(done, s)
			t.removeLocked(s)
		}
	}
	t.mu.Unlock()
	for _, s := range done {
		if s.reads == 0 {
			continue
		}
		ctx := oteltrace.ContextWithRemoteSpanContext(context.Background(), s.parent)
		_, span := t.tracer.Start(ctx, "snapshotter.reads", oteltrace.WithTimestamp(s.first), oteltrace.WithAttributes(
			attribute.Int("lazycloud.reads", s.reads), attribute.Int("lazycloud.cache_hits", s.hits),
			attribute.Int("lazycloud.fetches", s.misses), attribute.Int64("lazycloud.missed_bytes", s.missBytes),
			attribute.Float64("lazycloud.fetch_wait_seconds", s.missTime.Seconds()),
			attribute.Int64("lazycloud.fetch_wait_max_ms", s.maxMiss.Milliseconds())))
		span.End(oteltrace.WithTimestamp(s.last))
	}
}

// run flushes once a second until ctx ends.
func (t *startTraces) run(ctx context.Context) {
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			t.flush(time.Now().Add(traceLife))
			return
		case now := <-tick.C:
			t.flush(now)
		}
	}
}
