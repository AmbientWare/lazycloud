package snapshotter

import (
	"container/list"
	"context"
	"errors"
	"fmt"
	"hash/fnv"
	"io"
	"io/fs"
	"iter"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"golang.org/x/sync/singleflight"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
)

const (
	// fetchAttempts bounds the reads of one frame or index from the store;
	// the backoff runs between them.
	fetchAttempts = 3
	// fetchTimeout bounds all attempts at one frame together.
	fetchTimeout = 2 * time.Minute
)

// backoff is the wait before attempt n, counting from 1.
func backoff(n int) time.Duration { return 200 * time.Millisecond * time.Duration(n*n) }

// errNoGrant marks a read of a layer the agent has not granted, or whose
// grant expired.
var errNoGrant = errors.New("no live grant")

type frameKey struct {
	layer imagefs.Digest
	frame int
}

type cachedFrame struct {
	key  frameKey
	size int64
	// held says the frame is on the list of mounted layers' frames.
	held bool
}

// frameCache keeps uncompressed frames on local disk under a byte bound,
// evicting the least recently used first and frames of mounted layers only
// once no other frames are left. A layer's frames count as used when it
// mounts and when it unmounts. The store is the cache's durable source: it
// starts empty and refills on reads.
type frameCache struct {
	dir       string
	limit     int64
	fillBytes int64
	http      *http.Client
	log       *slog.Logger
	grants    *grants
	// traces records the frames read through mounts for the agent, and
	// starts sums them into the traces of the starts that read them.
	traces *tracer
	starts *startTraces
	// fetches makes concurrent reads of one frame share one fetch, and
	// slots bounds the fetches in flight and so the memory they hold.
	fetches singleflight.Group
	slots   chan struct{}
	// filling bounds the background fetches in flight, of mounted layers
	// and prefetches. They hold at most half of slots, so containers' reads
	// always find a slot free.
	filling chan struct{}
	// prefetches holds the running prefetches by name, at most
	// maxPrefetches. background waits for prefetches, fills and the start
	// traces' flushes.
	pmu        sync.Mutex
	prefetches map[string]*prefetchRun
	background sync.WaitGroup
	// life bounds every fetch: a fetch is shared, so no one reader's or
	// fill's context may end it.
	life context.Context
	// keys serialises writing and evicting the file of one frame, so an
	// eviction never deletes a frame stored again since.
	keys [64]sync.Mutex

	mu   sync.Mutex
	used int64
	// idle and held list the stored frames of unmounted and of mounted
	// layers, most recently used first.
	idle, held list.List
	frames     map[frameKey]*list.Element
	// live is the mounts' view the cache keeps: the layer the mounts of
	// each mounted digest share. mountWake closes when a digest mounts.
	live      map[imagefs.Digest]*layer
	mountWake chan struct{}
}

func newFrameCache(life context.Context, dir string, cfg Config) (*frameCache, error) {
	if err := os.RemoveAll(dir); err != nil {
		return nil, fmt.Errorf("clear frame cache: %w", err)
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("create frame cache: %w", err)
	}
	return &frameCache{
		dir: dir, limit: cfg.CacheBytes, fillBytes: cfg.FillBytes, http: cfg.HTTP, log: cfg.Logger,
		grants:     &grants{byLayer: make(map[imagefs.Digest]grant)},
		traces:     &tracer{traces: make(map[string]*trace)},
		starts:     &startTraces{tracer: cfg.Tracer, byName: map[string]*startTrace{}, byLayer: map[imagefs.Digest]*startTrace{}},
		slots:      make(chan struct{}, cfg.Fetches),
		filling:    make(chan struct{}, max(1, cfg.Fetches/2)),
		prefetches: make(map[string]*prefetchRun),
		life:       life,
		frames:     make(map[frameKey]*list.Element),
		live:       make(map[imagefs.Digest]*layer),
		mountWake:  make(chan struct{}),
	}, nil
}

func (c *frameCache) path(k frameKey) string {
	return filepath.Join(c.dir, strings.TrimPrefix(string(k.layer), "sha256:"), strconv.Itoa(k.frame))
}

// newLayer returns a layer ix describes, read through its current grant.
func (c *frameCache) newLayer(ix imagefs.Index) *layer {
	digest := ix.Layer
	l := &layer{
		digest: digest,
		index:  ix,
		data: imagefs.HTTPObject(c.http, func() string {
			g, _ := c.grants.lookup(digest)
			return g.dataURL
		}),
		cache:  c,
		paths:  make([]string, len(ix.Frames)),
		traced: make([]atomic.Uint32, len(ix.Frames)),
	}
	for i := range l.paths {
		l.paths[i] = c.path(frameKey{layer: digest, frame: i})
	}
	return l
}

// mount counts a mount of the layer ix describes and returns the layer the
// mounts of its digest share. The first mount of a digest starts its fill.
func (c *frameCache) mount(ix imagefs.Index) *layer {
	c.mu.Lock()
	l := c.live[ix.Layer]
	first := l == nil
	var fillCtx context.Context
	if first {
		l = c.newLayer(ix)
		fillCtx, l.stopFill = context.WithCancel(c.life)
		c.live[l.digest] = l
		c.moveFramesLocked(l, true)
		close(c.mountWake)
		c.mountWake = make(chan struct{})
	}
	l.mounts++
	c.mu.Unlock()
	if first {
		c.background.Go(func() { c.fillLayer(fillCtx, l) })
	}
	return l
}

// unmount counts an unmount of l, which mount returned.
func (c *frameCache) unmount(l *layer) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if l.mounts--; l.mounts > 0 {
		return
	}
	delete(c.live, l.digest)
	c.moveFramesLocked(l, false)
	l.stopFill()
}

// moveFramesLocked moves l's stored frames to the front of the list of
// mounted layers' frames, or of the others'.
func (c *frameCache) moveFramesLocked(l *layer, held bool) {
	from, to := c.list(!held), c.list(held)
	for i := range l.index.Frames {
		k := frameKey{layer: l.digest, frame: i}
		if e, ok := c.frames[k]; ok {
			f := from.Remove(e).(*cachedFrame) //nolint:forcetypeassert // the lists hold *cachedFrame
			f.held = held
			c.frames[k] = to.PushFront(f)
		}
	}
}

func (c *frameCache) list(held bool) *list.List {
	if held {
		return &c.held
	}
	return &c.idle
}

// read fills p with frame's bytes from off.
func (c *frameCache) read(l *layer, frame int, p []byte, off int64) error {
	c.traces.record(l, frame)
	// A frame evicted since the lookup, or unreadable, is fetched again.
	if _, ok := c.touch(frameKey{layer: l.digest, frame: frame}); ok && readAt(l.paths[frame], p, off) == nil {
		c.starts.read(l.digest, true, 0, 0)
		return nil
	}
	began := time.Now()
	data, err := c.load(l, frame) //nolint:contextcheck // a shared fetch runs under the cache's life
	if err != nil {
		return err
	}
	c.starts.read(l.digest, false, time.Since(began), len(data))
	if off+int64(len(p)) > int64(len(data)) {
		return fmt.Errorf("%w: read past frame %d of layer %s", imagefs.ErrInvalidIndex, frame, l.digest)
	}
	copy(p, data[off:])
	return nil
}

// fillLayer fetches a mounted layer within fillBytes whole, so a cold read
// waits for no store round trip per frame it touches.
func (c *frameCache) fillLayer(ctx context.Context, l *layer) {
	fills := l.index.StreamSize <= c.fillBytes
	span, traced := c.starts.start(l.digest, "snapshotter.fill", attribute.Int("lazycloud.frames", len(l.index.Frames)), attribute.Bool("lazycloud.fills", fills))
	var fetched int64
	if fills {
		fetched = c.loadEach(ctx, func(yield func(*layer, int) bool) {
			for i := range l.index.Frames {
				if !yield(l, i) {
					return
				}
			}
		})
	}
	if traced {
		span.SetAttributes(attribute.Int64("lazycloud.fetches", fetched))
		span.End()
	}
}

// loadEach loads the frames frames yields that the cache lacks, a few at a
// time and sharing fetches with reads, and returns how many it loaded. Once
// ctx ends it starts no more; the loads under way finish, since reads may
// be waiting on them.
func (c *frameCache) loadEach(ctx context.Context, frames iter.Seq2[*layer, int]) int64 {
	var loaded atomic.Int64
	var wg sync.WaitGroup
next:
	for l, i := range frames {
		if c.cached(frameKey{layer: l.digest, frame: i}) {
			continue
		}
		select {
		case c.filling <- struct{}{}:
		case <-ctx.Done():
			break next
		}
		wg.Go(func() { //nolint:contextcheck // a shared fetch runs under the cache's life
			defer func() { <-c.filling }()
			if _, err := c.load(l, i); err != nil {
				c.log.Debug("background fetch failed", "layer", l.digest, "frame", i, "error", err)
				return
			}
			loaded.Add(1)
		})
	}
	wg.Wait()
	return loaded.Load()
}

// load returns frame's bytes, fetching it once however many reads wait.
// The fetch runs under the cache's life and fetchTimeout alone.
func (c *frameCache) load(l *layer, frame int) ([]byte, error) {
	k := frameKey{layer: l.digest, frame: frame}
	v, err, _ := c.fetches.Do(string(l.digest)+"/"+strconv.Itoa(frame), func() (any, error) {
		// A fetch that just finished may have stored it.
		if size, ok := c.touch(k); ok {
			if data, err := os.ReadFile(l.paths[frame]); err == nil && int64(len(data)) == size { //nolint:gosec // a path under the cache directory
				return data, nil
			}
		}
		ctx, cancel := context.WithTimeout(c.life, fetchTimeout)
		defer cancel()
		select {
		case c.slots <- struct{}{}:
		case <-ctx.Done():
			return nil, fmt.Errorf("wait for a fetch slot: %w", ctx.Err())
		}
		defer func() { <-c.slots }()
		data, err := retry(ctx, func() ([]byte, error) {
			if _, ok := c.grants.lookup(l.digest); !ok {
				return nil, errNoGrant
			}
			return l.index.ReadFrame(ctx, l.data, frame) //nolint:wrapcheck // wrapped below
		})
		if err != nil {
			return nil, fmt.Errorf("read frame %d of layer %s: %w", frame, l.digest, err)
		}
		if err := c.write(k, data); err != nil {
			c.log.WarnContext(ctx, "frame cache write failed", "layer", l.digest, "frame", frame, "error", err)
		}
		c.evict()
		return data, nil
	})
	if err != nil {
		return nil, err //nolint:wrapcheck // wrapped inside the call
	}
	return v.([]byte), nil //nolint:forcetypeassert // the function returns []byte
}

// fetchIndex reads and checks a layer's stored index through its grant.
func (c *frameCache) fetchIndex(ctx context.Context, digest imagefs.Digest) ([]byte, imagefs.Index, error) {
	type fetched struct {
		raw []byte
		ix  imagefs.Index
	}
	got, err := retry(ctx, func() (fetched, error) {
		g, ok := c.grants.lookup(digest)
		if !ok {
			return fetched{}, errNoGrant
		}
		raw, ix, err := imagefs.FetchIndex(ctx, c.http, g.indexURL)
		if err == nil && ix.Layer != digest {
			err = fmt.Errorf("%w: the index granted for layer %s is layer %s's", imagefs.ErrInvalidIndex, digest, ix.Layer)
		}
		return fetched{raw: raw, ix: ix}, err //nolint:wrapcheck // wrapped below
	})
	if err != nil {
		return nil, imagefs.Index{}, fmt.Errorf("index of layer %s: %w", digest, err)
	}
	return got.raw, got.ix, nil
}

// retry runs attempt until it succeeds, fails for good or has run
// fetchAttempts times, with the backoff between attempts. Each attempt
// takes the layer's grant anew, so a refreshed URL applies at once.
func retry[T any](ctx context.Context, attempt func() (T, error)) (T, error) {
	var (
		v   T
		err error
	)
	for n := range fetchAttempts {
		if n > 0 && sleep(ctx, backoff(n)) != nil {
			break
		}
		if v, err = attempt(); err == nil || !retryable(err) {
			break
		}
	}
	return v, err
}

// retryable reports whether another attempt may succeed: a transport
// error, a store error, or a refused URL the agent may have refreshed.
func retryable(err error) bool {
	if errors.Is(err, errNoGrant) || errors.Is(err, imagefs.ErrInvalidIndex) || errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return false
	}
	var status *imagefs.StatusError
	if errors.As(err, &status) {
		return status.StatusCode >= 500 || status.StatusCode == 429 || status.StatusCode == 403 || status.StatusCode == 400
	}
	return true
}

func (c *frameCache) keyLock(k frameKey) *sync.Mutex {
	h := fnv.New32a()
	_, _ = h.Write([]byte(k.layer))
	return &c.keys[(h.Sum32()+uint32(k.frame))%uint32(len(c.keys))] //nolint:gosec // frame numbers are small and non-negative
}

// write stores k's file and counts it.
func (c *frameCache) write(k frameKey, data []byte) error {
	lock := c.keyLock(k)
	lock.Lock()
	defer lock.Unlock()
	path := c.path(k)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("store frame: %w", err)
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), "*.tmp")
	if err != nil {
		return fmt.Errorf("store frame: %w", err)
	}
	_, err = tmp.Write(data)
	if cerr := tmp.Close(); err == nil {
		err = cerr
	}
	if err == nil {
		err = os.Rename(tmp.Name(), path)
	}
	if err != nil {
		_ = os.Remove(tmp.Name())
		return fmt.Errorf("store frame: %w", err)
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if e, ok := c.frames[k]; ok {
		c.list(e.Value.(*cachedFrame).held).MoveToFront(e) //nolint:forcetypeassert // the lists hold *cachedFrame
	} else {
		c.linkLocked(&cachedFrame{key: k, size: int64(len(data))}, true)
	}
	return nil
}

// linkLocked counts f and lists it as the most recently used frame, or the
// least.
func (c *frameCache) linkLocked(f *cachedFrame, recent bool) {
	f.held = c.live[f.key.layer] != nil
	if recent {
		c.frames[f.key] = c.list(f.held).PushFront(f)
	} else {
		c.frames[f.key] = c.list(f.held).PushBack(f)
	}
	c.used += f.size
}

// evict drops frames until the cache fits its bound: unmounted layers'
// frames first, then mounted layers', least recently used first.
func (c *frameCache) evict() {
	var victims []*cachedFrame
	c.mu.Lock()
	for c.used > c.limit {
		from := &c.idle
		if from.Len() == 0 {
			from = &c.held
		}
		if from.Len() == 0 {
			break
		}
		f := from.Remove(from.Back()).(*cachedFrame) //nolint:forcetypeassert // the lists hold *cachedFrame
		delete(c.frames, f.key)
		c.used -= f.size
		victims = append(victims, f)
	}
	c.mu.Unlock()
	for _, f := range victims {
		if err := c.remove(f); err != nil {
			c.log.Warn("evicting a cached frame failed", "layer", f.key.layer, "frame", f.key.frame, "error", err)
		}
	}
}

// remove deletes an evicted frame's file unless it was stored again since.
// A file it cannot delete stays counted, first in line for the next
// eviction.
func (c *frameCache) remove(f *cachedFrame) error {
	lock := c.keyLock(f.key)
	lock.Lock()
	defer lock.Unlock()
	c.mu.Lock()
	_, back := c.frames[f.key]
	c.mu.Unlock()
	if back {
		return nil
	}
	err := os.Remove(c.path(f.key))
	if err == nil || errors.Is(err, fs.ErrNotExist) {
		return nil
	}
	c.mu.Lock()
	c.linkLocked(f, false)
	c.mu.Unlock()
	return fmt.Errorf("evict frame: %w", err)
}

// touch marks k used and returns its size if it is cached.
func (c *frameCache) touch(k frameKey) (int64, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.frames[k]
	if !ok {
		return 0, false
	}
	f := e.Value.(*cachedFrame) //nolint:forcetypeassert // the lists hold *cachedFrame
	c.list(f.held).MoveToFront(e)
	return f.size, true
}

// cached reports whether k is cached without marking it used.
func (c *frameCache) cached(k frameKey) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	_, ok := c.frames[k]
	return ok
}

func readAt(path string, p []byte, off int64) error {
	f, err := os.Open(path) //nolint:gosec // a path under the cache directory
	if err != nil {
		return fmt.Errorf("open cached frame: %w", err)
	}
	defer func() { _ = f.Close() }()
	if _, err := f.ReadAt(p, off); err != nil {
		if errors.Is(err, io.EOF) {
			return fmt.Errorf("cached frame %s is short", path)
		}
		return fmt.Errorf("read cached frame: %w", err)
	}
	return nil
}

func sleep(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return nil
	case <-ctx.Done():
		return ctx.Err() //nolint:wrapcheck // the caller's own context error
	}
}
