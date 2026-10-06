package snapshotter

import (
	"container/list"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"iter"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"golang.org/x/sync/singleflight"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
)

const (
	// fetchAttempts bounds the reads of one frame or index from the store.
	fetchAttempts = 3
	// fetchTimeout bounds all attempts at one frame together.
	fetchTimeout = 2 * time.Minute
)

// errNoGrant marks a read of a layer the agent has not granted, or whose
// grant expired.
var errNoGrant = errors.New("no live grant")

type frameKey struct {
	layer imagefs.Digest
	frame int
}

// cachedFrame is one stored frame. Every store writes a new file, so an
// eviction deletes only the file it chose.
type cachedFrame struct {
	key  frameKey
	path string
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
	// fetches makes concurrent reads of one frame share one fetch, slots
	// bounds the fetches in flight and so the memory they hold, and reader
	// decodes as many frames at once.
	fetches singleflight.Group
	slots   chan struct{}
	reader  *imagefs.FrameReader
	// filling bounds the background fetches in flight, of mounted layers
	// and prefetches. They hold at most half of slots, so containers' reads
	// always find a slot free.
	filling chan struct{}
	// prefetches holds the stops of the running prefetches by name, at most
	// maxPrefetches. background waits for prefetches, fills and the start
	// traces' flushes.
	pmu        sync.Mutex
	prefetches map[string]*context.CancelFunc
	background sync.WaitGroup
	// life bounds every fetch: a fetch is shared, so no one reader's or
	// fill's context may end it.
	life context.Context

	mu   sync.Mutex
	used int64
	// idle and held list the stored frames of unmounted and of mounted
	// layers, most recently used first. A frame whose file an eviction
	// failed to delete stays listed, and counted, without a key in frames.
	idle, held list.List
	frames     map[frameKey]*list.Element
	// live is the cache's view of the mounts: the layer the mounts of each
	// mounted digest share. mountWake closes when a digest mounts.
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
	reader, err := imagefs.NewFrameReader(cfg.Fetches)
	if err != nil {
		return nil, err //nolint:wrapcheck // the decoder names itself
	}
	return &frameCache{
		dir: dir, limit: cfg.CacheBytes, fillBytes: cfg.FillBytes, http: cfg.HTTP, log: cfg.Logger, reader: reader,
		grants:     &grants{byLayer: make(map[imagefs.Digest]grant)},
		traces:     &tracer{traces: make(map[string]*trace)},
		starts:     &startTraces{tracer: cfg.Tracer, byName: map[string]*startTrace{}, byLayer: map[imagefs.Digest]*startTrace{}},
		slots:      make(chan struct{}, cfg.Fetches),
		filling:    make(chan struct{}, max(1, cfg.Fetches/2)),
		prefetches: make(map[string]*context.CancelFunc),
		life:       life,
		frames:     make(map[frameKey]*list.Element),
		live:       make(map[imagefs.Digest]*layer),
		mountWake:  make(chan struct{}),
	}, nil
}

// close waits for the background work and releases the decoder.
func (c *frameCache) close() {
	c.background.Wait()
	c.reader.Close()
}

// newLayer returns a layer ix describes, read through its current grant.
func (c *frameCache) newLayer(ix imagefs.Index) *layer {
	return &layer{
		digest: ix.Layer,
		index:  ix,
		data: imagefs.HTTPObject(c.http, func() string {
			g, _ := c.grants.lookup(ix.Layer)
			return g.dataURL
		}),
		cache:  c,
		traced: make([]atomic.Uint32, len(ix.Frames)),
	}
}

// mount counts a mount of the layer ix describes and returns the layer the
// mounts of its digest share. The first mount of a digest starts its fill.
func (c *frameCache) mount(ix imagefs.Index) *layer {
	c.mu.Lock()
	defer c.mu.Unlock()
	if l := c.live[ix.Layer]; l != nil {
		l.mounts++
		return l
	}
	l := c.newLayer(ix)
	l.mounts = 1
	var fill context.Context
	fill, l.stopFill = context.WithCancel(c.life)
	c.live[l.digest] = l
	c.moveFramesLocked(l, true)
	close(c.mountWake)
	c.mountWake = make(chan struct{})
	c.background.Go(func() { c.fillLayer(fill, l) })
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
	for i := range l.index.Frames {
		k := frameKey{layer: l.digest, frame: i}
		if e, ok := c.frames[k]; ok {
			f := c.list(!held).Remove(e).(*cachedFrame) //nolint:forcetypeassert // the lists hold *cachedFrame
			f.held = held
			c.frames[k] = c.list(held).PushFront(f)
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
	if f := c.touch(frameKey{layer: l.digest, frame: frame}); f != nil && readAt(f.path, p, off) == nil {
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
		c.mu.Lock()
		_, cached := c.frames[frameKey{layer: l.digest, frame: i}]
		c.mu.Unlock()
		if cached {
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
		if f := c.touch(k); f != nil {
			if data, err := os.ReadFile(f.path); err == nil && len(data) == l.index.FrameLen(frame) {
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
		var data []byte
		err := retry(ctx, func() (err error) {
			if _, ok := c.grants.lookup(l.digest); !ok {
				return errNoGrant
			}
			data, err = c.reader.Read(ctx, l.index, l.data, frame)
			return err //nolint:wrapcheck // wrapped below
		})
		if err != nil {
			return nil, fmt.Errorf("read frame %d of layer %s: %w", frame, l.digest, err)
		}
		if err := c.store(k, data); err != nil {
			c.log.WarnContext(ctx, "frame cache write failed", "layer", l.digest, "frame", frame, "error", err)
		}
		return data, nil
	})
	if err != nil {
		return nil, err //nolint:wrapcheck // wrapped inside the call
	}
	return v.([]byte), nil //nolint:forcetypeassert // the function returns []byte
}

// retry runs attempt until it succeeds, fails for good or has run
// fetchAttempts times, backing off between attempts. Each attempt takes the
// layer's grant anew, so a refreshed URL applies at once.
func retry(ctx context.Context, attempt func() error) error {
	var err error
	for n := range fetchAttempts {
		if n > 0 {
			select {
			case <-time.After(200 * time.Millisecond * time.Duration(n*n)):
			case <-ctx.Done():
				return err
			}
		}
		if err = attempt(); err == nil || !retryable(err) {
			return err
		}
	}
	return err
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

// store writes a fetched frame to a new file, lists it in place of any
// copy, and evicts down to the bound. The frame's bytes reach their readers
// whether or not it is stored.
func (c *frameCache) store(k frameKey, data []byte) error {
	file, err := os.CreateTemp(c.dir, "frame-")
	if err != nil {
		return fmt.Errorf("store frame: %w", err)
	}
	_, err = file.Write(data)
	if cerr := file.Close(); err == nil {
		err = cerr
	}
	if err != nil {
		_ = os.Remove(file.Name())
		return fmt.Errorf("store frame: %w", err)
	}
	c.mu.Lock()
	var victims []*cachedFrame
	if e, ok := c.frames[k]; ok {
		victims = append(victims, c.unlinkLocked(e))
	}
	c.linkLocked(&cachedFrame{key: k, path: file.Name(), size: int64(len(data))}, true)
	for c.used > c.limit {
		from := &c.idle
		if from.Len() == 0 {
			from = &c.held
		}
		if from.Len() == 0 {
			break
		}
		victims = append(victims, c.unlinkLocked(from.Back()))
	}
	c.mu.Unlock()
	c.remove(victims)
	return nil
}

// remove deletes the files of unlisted frames. A file it cannot delete is
// listed and counted again, oldest, so the next eviction tries it first.
func (c *frameCache) remove(frames []*cachedFrame) {
	for _, f := range frames {
		if err := os.Remove(f.path); err != nil && !errors.Is(err, fs.ErrNotExist) {
			c.log.Warn("evicting a cached frame failed", "layer", f.key.layer, "frame", f.key.frame, "error", err)
			c.mu.Lock()
			c.linkLocked(f, false)
			c.mu.Unlock()
		}
	}
}

// linkLocked counts f and lists it as the most recently used frame, or the
// least. It is found by key unless another copy is.
func (c *frameCache) linkLocked(f *cachedFrame, recent bool) {
	f.held = c.live[f.key.layer] != nil
	if recent {
		c.frames[f.key] = c.list(f.held).PushFront(f)
	} else if e := c.list(f.held).PushBack(f); c.frames[f.key] == nil {
		c.frames[f.key] = e
	}
	c.used += f.size
}

func (c *frameCache) unlinkLocked(e *list.Element) *cachedFrame {
	f := e.Value.(*cachedFrame) //nolint:forcetypeassert // the lists hold *cachedFrame
	c.list(f.held).Remove(e)
	if c.frames[f.key] == e {
		delete(c.frames, f.key)
	}
	c.used -= f.size
	return f
}

// touch marks k used and returns it if it is stored.
func (c *frameCache) touch(k frameKey) *cachedFrame {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.frames[k]
	if !ok {
		return nil
	}
	f := e.Value.(*cachedFrame) //nolint:forcetypeassert // the lists hold *cachedFrame
	c.list(f.held).MoveToFront(e)
	return f
}

func readAt(path string, p []byte, off int64) error {
	f, err := os.Open(path) //nolint:gosec // a file of the cache
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
