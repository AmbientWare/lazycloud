package snapshotter

import (
	"context"
	"slices"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel/attribute"
	oteltrace "go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
)

const (
	// maxTraceReads bounds the frames one trace records and one prefetch
	// names: 16 GiB uncompressed, past what a startup reads.
	maxTraceReads = 4096
	// maxTraces bounds the traces recording at once, and maxName the name
	// of a trace or prefetch.
	maxTraces = 64
	maxName   = 128
	// traceLife drops a trace nobody ended, as when the agent restarted
	// while its container started.
	traceLife = 15 * time.Minute
	// maxPrefetches bounds the prefetches running at once.
	maxPrefetches = 16
	// prefetchLife bounds one prefetch, and prefetchMountWait how long it
	// waits for its layers to mount: a start mounts them within seconds of
	// its prefetch, so a layer not mounted by then belongs to a start that
	// failed or went elsewhere.
	prefetchLife      = 5 * time.Minute
	prefetchMountWait = 30 * time.Second
)

// frameRead is a frame of the layer at a position in an image's layers.
type frameRead struct {
	layer uint32
	frame uint32
}

// trace is the frames read from some layers since it started.
type trace struct {
	layers   map[imagefs.Digest]uint32
	seen     map[frameKey]struct{}
	reads    []frameRead
	complete bool
	expires  time.Time
}

// tracer records, for each running trace, the frames read through mounts
// of its layers, each the first time. A FUSE read cannot be tied to a
// container, so a trace is complete only while its layers are its own: none
// was mounted when it started, and no other start has named one since. A
// read also reaches FUSE only when the page cache of its mount misses.
type tracer struct {
	// gen counts the traces started. A frame recorded since the latest
	// start is in every running trace of its layer, so reading it again
	// takes no lock.
	gen atomic.Uint32

	mu     sync.Mutex
	traces map[string]*trace
}

// expireLocked drops traces past their life.
func (t *tracer) expireLocked(now time.Time) {
	for name, tr := range t.traces {
		if !now.Before(tr.expires) {
			delete(t.traces, name)
		}
	}
}

// record adds a read of frame of l to the traces of l.
func (t *tracer) record(l *layer, frame int) {
	if l.traced[frame].Load() == t.gen.Load() {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	t.expireLocked(time.Now())
	k := frameKey{layer: l.digest, frame: frame}
	for _, tr := range t.traces {
		position, ok := tr.layers[l.digest]
		if !ok || len(tr.reads) >= maxTraceReads {
			continue
		}
		if _, read := tr.seen[k]; read {
			continue
		}
		tr.seen[k] = struct{}{}
		tr.reads = append(tr.reads, frameRead{layer: position, frame: uint32(frame)}) //nolint:gosec // frame numbers are below imagefs's frame bound
	}
	l.traced[frame].Store(t.gen.Load())
}

// claim marks incomplete every running trace other than name's that shares
// one of layers: another start uses them, and its reads would be counted.
func (t *tracer) claim(name string, layers []imagefs.Digest) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.claimLocked(name, layers)
}

// claimLocked reports whether another trace shares one of layers.
func (t *tracer) claimLocked(name string, layers []imagefs.Digest) bool {
	shared := false
	for other, tr := range t.traces {
		if other != name && slices.ContainsFunc(layers, func(l imagefs.Digest) bool { _, ok := tr.layers[l]; return ok }) {
			tr.complete = false
			shared = true
		}
	}
	return shared
}

// start begins the trace name of layers, replacing a running one of that
// name. complete says no layer is mounted yet; it is also not complete if
// another running trace shares a layer, and then neither is.
func (t *tracer) start(name string, layers []imagefs.Digest, complete bool) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.expireLocked(time.Now())
	if _, ok := t.traces[name]; !ok && len(t.traces) >= maxTraces {
		return status.Errorf(codes.ResourceExhausted, "%d traces are recording", len(t.traces))
	}
	delete(t.traces, name)
	tr := &trace{
		layers: make(map[imagefs.Digest]uint32, len(layers)), seen: make(map[frameKey]struct{}),
		expires: time.Now().Add(traceLife),
	}
	for i, l := range layers {
		if _, ok := tr.layers[l]; !ok {
			tr.layers[l] = uint32(i) //nolint:gosec // at most maxGrantsPerCall layers
		}
	}
	tr.complete = !t.claimLocked(name, layers) && complete
	t.traces[name] = tr
	t.gen.Add(1)
	return nil
}

// end stops the trace name and returns it, or false if it is not running.
func (t *tracer) end(name string) (*trace, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.expireLocked(time.Now())
	tr, ok := t.traces[name]
	delete(t.traces, name)
	return tr, ok
}

// startTrace begins a trace of layers, complete if none is mounted.
func (c *frameCache) startTrace(name string, layers []imagefs.Digest) error {
	c.mu.Lock()
	complete := !slices.ContainsFunc(layers, func(l imagefs.Digest) bool { return c.live[l] != nil })
	c.mu.Unlock()
	return c.traces.start(name, layers, complete)
}

// prefetchRun is a running prefetch; stop ends it.
type prefetchRun struct {
	stop context.CancelFunc
}

// prefetch fetches reads in order in the background, each once its layer
// is mounted, sharing fetches with reads and fills, until stopPrefetch of
// name. It holds at most a quarter of the cache, so it never evicts most of
// what containers use.
func (c *frameCache) prefetch(name string, reads []frameKey, parent oteltrace.SpanContext) error {
	reads = reads[:min(len(reads), int(c.limit/imagefs.FrameSize/4))]
	ctx, cancel := context.WithTimeout(c.life, prefetchLife)
	run := &prefetchRun{stop: cancel}
	c.pmu.Lock()
	if old, ok := c.prefetches[name]; ok {
		old.stop()
	} else if len(c.prefetches) >= maxPrefetches {
		c.pmu.Unlock()
		cancel()
		return status.Errorf(codes.ResourceExhausted, "%d prefetches are running", maxPrefetches)
	}
	c.prefetches[name] = run
	c.pmu.Unlock()
	c.background.Go(func() { //nolint:contextcheck // a prefetch lives with the cache, not the call
		defer func() {
			cancel()
			c.pmu.Lock()
			if c.prefetches[name] == run {
				delete(c.prefetches, name)
			}
			c.pmu.Unlock()
		}()
		began := time.Now()
		var span oteltrace.Span
		if parent.IsSampled() {
			span = c.starts.span(parent, "snapshotter.prefetch", oteltrace.WithAttributes(attribute.Int("lazycloud.frames", len(reads))))
		}
		fetched := c.runPrefetch(ctx, reads)
		if span != nil {
			span.SetAttributes(attribute.Int64("lazycloud.fetches", fetched), attribute.Bool("lazycloud.stopped", ctx.Err() != nil))
			span.End()
		}
		c.log.Info("prefetch ended", "frames", len(reads), "fetched", fetched, "seconds", time.Since(began).Seconds(), "error", ctx.Err())
	})
	return nil
}

// stopPrefetch ends the prefetch name, if it runs.
func (c *frameCache) stopPrefetch(name string) {
	c.pmu.Lock()
	defer c.pmu.Unlock()
	if run, ok := c.prefetches[name]; ok {
		run.stop()
		delete(c.prefetches, name)
	}
}

// runPrefetch loads reads as their layers mount and returns how many
// frames it fetched.
func (c *frameCache) runPrefetch(ctx context.Context, reads []frameKey) int64 {
	mounting, stopWaiting := context.WithTimeout(ctx, prefetchMountWait)
	defer stopWaiting()
	// seen holds the layers found mounted; one gone since is skipped.
	seen := make(map[imagefs.Digest]bool)
	return c.loadEach(ctx, func(yield func(*layer, int) bool) {
		for _, r := range reads {
			l := c.awaitMount(mounting, r.layer, seen)
			if ctx.Err() != nil {
				return
			}
			if l != nil && r.frame < len(l.index.Frames) && !yield(l, r.frame) {
				return
			}
		}
	})
}

// awaitMount returns the mounted layer of digest, waiting for it to mount
// until ctx ends. It returns nil for a layer seen mounted and gone since.
func (c *frameCache) awaitMount(ctx context.Context, digest imagefs.Digest, seen map[imagefs.Digest]bool) *layer {
	for {
		c.mu.Lock()
		l, wake := c.live[digest], c.mountWake
		c.mu.Unlock()
		if l != nil {
			seen[digest] = true
			return l
		}
		if seen[digest] {
			return nil
		}
		select {
		case <-wake:
		case <-ctx.Done():
			return nil
		}
	}
}
