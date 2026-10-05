package snapshotter

import (
	"context"
	"fmt"
	"slices"
	"sync"
	"sync/atomic"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
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
	life time.Duration
	// running counts traces so reads skip the lock when there are none.
	running atomic.Int32

	mu     sync.Mutex
	traces map[string]*trace
}

func newTracer(life time.Duration) *tracer {
	return &tracer{life: life, traces: make(map[string]*trace)}
}

// expireLocked drops traces past their life.
func (t *tracer) expireLocked(now time.Time) {
	for name, tr := range t.traces {
		if !now.Before(tr.expires) {
			delete(t.traces, name)
		}
	}
	t.running.Store(int32(len(t.traces))) //nolint:gosec // at most maxTraces
}

// record adds a read of frame of layer to the traces of layer.
func (t *tracer) record(layer imagefs.Digest, frame int) {
	if t.running.Load() == 0 {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	t.expireLocked(time.Now())
	k := frameKey{layer: layer, frame: frame}
	for _, tr := range t.traces {
		position, ok := tr.layers[layer]
		if !ok || len(tr.reads) >= maxTraceReads {
			continue
		}
		if _, read := tr.seen[k]; read {
			continue
		}
		tr.seen[k] = struct{}{}
		tr.reads = append(tr.reads, frameRead{layer: position, frame: uint32(frame)}) //nolint:gosec // frame numbers are below imagefs's frame bound
	}
}

// claim marks incomplete every running trace other than name's that shares
// one of layers: another start uses them, and its reads would be counted.
func (t *tracer) claim(name string, layers []imagefs.Digest) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.claimLocked(name, layers)
}

func (t *tracer) claimLocked(name string, layers []imagefs.Digest) {
	for other, tr := range t.traces {
		if other == name && name != "" {
			continue
		}
		if slices.ContainsFunc(layers, func(l imagefs.Digest) bool { _, ok := tr.layers[l]; return ok }) {
			tr.complete = false
		}
	}
}

// start begins the trace name of layers, replacing a running one of that
// name. complete says no layer is mounted yet; it is also not complete if
// another running trace shares a layer, and that trace is no longer.
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
		expires: time.Now().Add(t.life),
	}
	for i, l := range layers {
		if _, ok := tr.layers[l]; !ok {
			tr.layers[l] = uint32(i) //nolint:gosec // at most maxGrantsPerCall layers
		}
	}
	shared := false
	for _, other := range t.traces {
		shared = shared || slices.ContainsFunc(layers, func(l imagefs.Digest) bool { _, ok := other.layers[l]; return ok })
	}
	t.claimLocked(name, layers)
	tr.complete = complete && !shared
	t.traces[name] = tr
	t.running.Store(int32(len(t.traces))) //nolint:gosec // at most maxTraces
	return nil
}

// end stops the trace name and returns it, or false if it is not running.
func (t *tracer) end(name string) (*trace, bool) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.expireLocked(time.Now())
	tr, ok := t.traces[name]
	delete(t.traces, name)
	t.running.Store(int32(len(t.traces))) //nolint:gosec // at most maxTraces
	return tr, ok
}

// startTrace begins a trace of layers, complete if none is mounted.
func (c *frameCache) startTrace(name string, layers []imagefs.Digest) error {
	c.mu.Lock()
	complete := true
	for _, l := range layers {
		complete = complete && c.mounted[l] == 0
	}
	c.mu.Unlock()
	return c.traces.start(name, layers, complete)
}

// prefetchRead is a frame a prefetch fetches.
type prefetchRead struct {
	layer imagefs.Digest
	frame int
}

// prefetchRun is a running prefetch; stop ends it.
type prefetchRun struct {
	stop context.CancelFunc
}

// prefetch fetches reads in order in the background, each once its layer
// is mounted, sharing fetches with reads and fills, until stopPrefetch of
// name. It holds at most a quarter of the cache, so it never evicts most of
// what containers use.
func (c *frameCache) prefetch(name string, reads []prefetchRead) error {
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
		c.runPrefetch(ctx, reads)
		c.log.Info("prefetch ended", "frames", len(reads), "seconds", time.Since(began).Seconds(), "error", ctx.Err())
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

func (c *frameCache) runPrefetch(ctx context.Context, reads []prefetchRead) {
	var wg sync.WaitGroup
	defer wg.Wait()
	mounting, stopWaiting := context.WithTimeout(ctx, prefetchMountWait)
	defer stopWaiting()
	// seen holds the layers found mounted; one gone since is skipped.
	seen := make(map[imagefs.Digest]bool)
	for _, r := range reads {
		l := c.awaitMount(mounting, r.layer, seen)
		if ctx.Err() != nil {
			return
		}
		if l == nil || r.frame >= len(l.index.Frames) {
			continue
		}
		c.mu.Lock()
		_, cached := c.frames[frameKey{layer: l.digest, frame: r.frame}]
		c.mu.Unlock()
		if cached {
			continue
		}
		select {
		case c.filling <- struct{}{}:
		case <-ctx.Done():
			return
		}
		wg.Go(func() { //nolint:contextcheck // a shared fetch runs under the cache's life
			defer func() { <-c.filling }()
			if _, err := c.load(l, r.frame); err != nil {
				c.log.Debug("prefetch failed", "layer", l.digest, "frame", r.frame, "error", err)
				return
			}
			c.metrics.framesPrefetched.Inc()
		})
	}
}

// awaitMount returns a mounted layer of digest, waiting for it to mount
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

// digestsIn checks and converts the diff_ids of a request.
func digestsIn(layers []string) ([]imagefs.Digest, error) {
	if len(layers) > maxGrantsPerCall {
		return nil, status.Errorf(codes.InvalidArgument, "a call names at most %d layers", maxGrantsPerCall)
	}
	out := make([]imagefs.Digest, len(layers))
	for i, raw := range layers {
		out[i] = imagefs.Digest(raw)
		if err := out[i].Check(); err != nil {
			return nil, status.Error(codes.InvalidArgument, err.Error())
		}
	}
	return out, nil
}

func checkName(name string) error {
	if name == "" || len(name) > maxName {
		return status.Errorf(codes.InvalidArgument, "a name has 1 to %d bytes", maxName)
	}
	return nil
}

func (s layerSources) Prefetch(_ context.Context, request *imagefsproto.PrefetchRequest) (*imagefsproto.PrefetchResponse, error) {
	if err := checkName(request.GetName()); err != nil {
		return nil, err
	}
	layers, err := digestsIn(request.GetLayers())
	if err != nil {
		return nil, err
	}
	if len(request.GetReads()) > maxTraceReads {
		return nil, status.Errorf(codes.InvalidArgument, "a prefetch names at most %d frames", maxTraceReads)
	}
	reads := make([]prefetchRead, len(request.GetReads()))
	for i, r := range request.GetReads() {
		if int(r.GetLayer()) >= len(layers) {
			return nil, status.Errorf(codes.InvalidArgument, "read %d names layer %d of %d", i, r.GetLayer(), len(layers))
		}
		reads[i] = prefetchRead{layer: layers[r.GetLayer()], frame: int(r.GetFrame())}
	}
	s.frames.traces.claim(request.GetName(), layers)
	if err := s.frames.prefetch(request.GetName(), reads); err != nil { //nolint:contextcheck // a prefetch lives with the cache, not the call
		return nil, err
	}
	return &imagefsproto.PrefetchResponse{}, nil
}

func (s layerSources) StopPrefetch(_ context.Context, request *imagefsproto.StopPrefetchRequest) (*imagefsproto.StopPrefetchResponse, error) {
	s.frames.stopPrefetch(request.GetName())
	return &imagefsproto.StopPrefetchResponse{}, nil
}

func (s layerSources) StartTrace(_ context.Context, request *imagefsproto.StartTraceRequest) (*imagefsproto.StartTraceResponse, error) {
	if err := checkName(request.GetName()); err != nil {
		return nil, err
	}
	layers, err := digestsIn(request.GetLayers())
	if err != nil {
		return nil, err
	}
	if err := s.frames.startTrace(request.GetName(), layers); err != nil {
		return nil, err
	}
	return &imagefsproto.StartTraceResponse{}, nil
}

func (s layerSources) EndTrace(_ context.Context, request *imagefsproto.EndTraceRequest) (*imagefsproto.EndTraceResponse, error) {
	tr, ok := s.frames.traces.end(request.GetName())
	if !ok {
		return nil, status.Error(codes.NotFound, fmt.Sprintf("no trace %q is recording", request.GetName()))
	}
	out := &imagefsproto.EndTraceResponse{Complete: tr.complete, Reads: make([]*imagefsproto.FrameRead, len(tr.reads))}
	for i, r := range tr.reads {
		out.Reads[i] = &imagefsproto.FrameRead{Layer: r.layer, Frame: r.frame}
	}
	return out, nil
}
