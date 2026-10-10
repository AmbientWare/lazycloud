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
	"golang.org/x/sys/unix"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
)

const (
	// fetchAttempts bounds the reads of one frame or index from the store.
	fetchAttempts = 3
	// fetchTimeout bounds all attempts at one frame together.
	fetchTimeout = 2 * time.Minute
	// trimEvery is how often the cache checks the free space its volume
	// keeps for disks' unpublished writes.
	trimEvery = 5 * time.Second
)

// errNoGrant marks a read of a layer or disk the agent has not granted, or
// whose grant expired.
var errNoGrant = errors.New("no live grant")

// frameKey names one stored frame: frame of an image layer's data object,
// or frame 0 of a stored disk frame, whose object names its content.
type frameKey struct {
	object string
	frame  int
}

// frameSource is a stored object whose frames the cache keeps: an image
// layer's data, or a generation of a disk.
type frameSource interface {
	key(frame int) frameKey
	frameLen(frame int) int
	// fetch reads frame from the store, trying again while that may help.
	fetch(ctx context.Context, frame int) ([]byte, error)
}

// cachedFrame is one stored frame. Every store writes a new file, so an
// eviction deletes only the file it chose.
type cachedFrame struct {
	key  frameKey
	path string
	size int64
	// held says the frame is on the list of held frames.
	held bool
}

// frameCache keeps uncompressed frames on local disk under a byte bound,
// and below it while the volume's free space is under reserve, which the
// disk engine's unpublished writes need. It evicts the least recently used
// first, and frames of mounted layers and served disks only once no other
// frames are left. A frame counts as used when it becomes held and when it
// stops being held. The store is the cache's durable source: it starts
// empty and refills on reads.
type frameCache struct {
	dir       string
	limit     int64
	reserve   int64
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
	// filling bounds the background fetches in flight: fills, prefetches
	// and warms. They hold at most half of slots, so containers' reads
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
	// idle and held list the stored frames, most recently used first. A
	// frame whose file an eviction failed to delete stays listed, and
	// counted, without a key in frames.
	idle, held list.List
	frames     map[frameKey]*list.Element
	// holds counts, per frame, the mounted layers and served disk
	// generations that hold it.
	holds map[frameKey]int
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
		dir: dir, limit: cfg.CacheBytes, reserve: cfg.ReserveBytes, fillBytes: cfg.FillBytes, http: cfg.HTTP, log: cfg.Logger, reader: reader,
		grants:     &grants{byLayer: make(map[imagefs.Digest]grant)},
		traces:     &tracer{traces: make(map[string]*trace)},
		starts:     &startTraces{tracer: cfg.Tracer, byName: map[string]*startTrace{}, byLayer: map[imagefs.Digest]*startTrace{}},
		slots:      make(chan struct{}, cfg.Fetches),
		filling:    make(chan struct{}, max(1, cfg.Fetches/2)),
		prefetches: make(map[string]*context.CancelFunc),
		life:       life,
		frames:     make(map[frameKey]*list.Element),
		holds:      make(map[frameKey]int),
		live:       make(map[imagefs.Digest]*layer),
		mountWake:  make(chan struct{}),
	}, nil
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
	c.holdLocked(l.keys(), 1)
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
	c.holdLocked(l.keys(), -1)
	l.stopFill()
}

// hold counts a hold on each of keys, or with -1 drops one.
func (c *frameCache) hold(keys iter.Seq[frameKey], delta int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.holdLocked(keys, delta)
}

// holdLocked counts delta holds on each of keys, moving a stored frame that
// becomes held, or stops being held, to the front of its new list.
func (c *frameCache) holdLocked(keys iter.Seq[frameKey], delta int) {
	for k := range keys {
		was := c.holds[k] > 0
		if c.holds[k] += delta; c.holds[k] <= 0 {
			delete(c.holds, k)
		}
		held := c.holds[k] > 0
		if e, ok := c.frames[k]; ok && held != was {
			f := c.list(was).Remove(e).(*cachedFrame) //nolint:forcetypeassert // the lists hold *cachedFrame
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

// cached reports whether k is stored.
func (c *frameCache) cached(k frameKey) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	_, ok := c.frames[k]
	return ok
}

// read fills p with frame's bytes from off and reports whether the cache
// held it, or how long its fetch was waited for.
func (c *frameCache) read(src frameSource, frame int, p []byte, off int64) (bool, time.Duration, error) {
	// A frame evicted since the lookup, or unreadable, is fetched again.
	if f := c.touch(src.key(frame)); f != nil && readAt(f.path, p, off) == nil {
		return true, 0, nil
	}
	began := time.Now()
	data, err := c.load(src, frame) //nolint:contextcheck // a shared fetch runs under the cache's life
	if err != nil {
		return false, 0, err
	}
	if off+int64(len(p)) > int64(len(data)) {
		return false, 0, fmt.Errorf("%w: read past frame %d of %s", imagefs.ErrInvalidIndex, frame, src.key(frame).object)
	}
	copy(p, data[off:])
	return false, time.Since(began), nil
}

// fillLayer fetches a mounted layer within fillBytes whole, so a cold read
// waits for no store round trip per frame it touches.
func (c *frameCache) fillLayer(ctx context.Context, l *layer) {
	fills := l.index.StreamSize <= c.fillBytes
	span, traced := c.starts.start(l.digest, "snapshotter.fill", attribute.Int("lazycloud.frames", len(l.index.Frames)), attribute.Bool("lazycloud.fills", fills))
	var fetched int64
	if fills {
		fetched = c.loadEach(ctx, func(yield func(frameSource, int) bool) {
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
func (c *frameCache) loadEach(ctx context.Context, frames iter.Seq2[frameSource, int]) int64 {
	var loaded atomic.Int64
	var wg sync.WaitGroup
next:
	for src, i := range frames {
		if c.cached(src.key(i)) {
			continue
		}
		select {
		case c.filling <- struct{}{}:
		case <-ctx.Done():
			break next
		}
		wg.Go(func() { //nolint:contextcheck // a shared fetch runs under the cache's life
			defer func() { <-c.filling }()
			if _, err := c.load(src, i); err != nil {
				c.log.Debug("background fetch failed", "object", src.key(i).object, "frame", i, "error", err)
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
func (c *frameCache) load(src frameSource, frame int) ([]byte, error) {
	k := src.key(frame)
	v, err, _ := c.fetches.Do(k.object+"/"+strconv.Itoa(k.frame), func() (any, error) {
		// A fetch that just finished may have stored it.
		if f := c.touch(k); f != nil {
			if data, err := os.ReadFile(f.path); err == nil && len(data) == src.frameLen(frame) {
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
		data, err := src.fetch(ctx, frame)
		if err != nil {
			return nil, err
		}
		if err := c.store(k, data); err != nil {
			c.log.WarnContext(ctx, "frame cache write failed", "object", k.object, "frame", k.frame, "error", err)
		}
		return data, nil
	})
	if err != nil {
		return nil, err //nolint:wrapcheck // the source names the frame
	}
	return v.([]byte), nil //nolint:forcetypeassert // the function returns []byte
}

// retry runs attempt until it succeeds, fails for good or has run
// fetchAttempts times, backing off between attempts. Each attempt takes the
// current grant anew, so a refreshed one applies at once.
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
// error, a store error, or a refused credential the agent may have
// refreshed.
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
// copy, and evicts down to the bounds. The frame's bytes reach their
// readers whether or not it is stored.
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
	victims = c.evictLocked(victims)
	c.mu.Unlock()
	c.remove(victims)
	return nil
}

// evictLocked unlists frames, least recently used first, until the cache
// is within its bound and the volume has reserve free, and returns them
// with victims for remove.
func (c *frameCache) evictLocked(victims []*cachedFrame) []*cachedFrame {
	short := c.reserve - c.freeBytes()
	for c.used > c.limit || short > 0 {
		from := &c.idle
		if from.Len() == 0 {
			from = &c.held
		}
		if from.Len() == 0 {
			break
		}
		f := c.unlinkLocked(from.Back())
		short -= f.size
		victims = append(victims, f)
	}
	return victims
}

// freeBytes is the space the cache's volume has free, or the reserve when
// it cannot be read, so a failed read evicts nothing.
func (c *frameCache) freeBytes() int64 {
	var st unix.Statfs_t
	if err := unix.Statfs(c.dir, &st); err != nil {
		c.log.Warn("reading the cache volume's free space failed", "error", err)
		return c.reserve
	}
	return int64(st.Bavail) * st.Bsize //nolint:gosec // block counts fit an int64
}

// trim evicts every trimEvery while the volume is short of its reserve,
// which disks' writes use up between fetches, until ctx ends.
func (c *frameCache) trim(ctx context.Context) {
	tick := time.NewTicker(trimEvery)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		}
		c.mu.Lock()
		victims := c.evictLocked(nil)
		c.mu.Unlock()
		c.remove(victims)
	}
}

// remove deletes the files of unlisted frames. A file it cannot delete is
// listed and counted again, oldest, so the next eviction tries it first.
func (c *frameCache) remove(frames []*cachedFrame) {
	for _, f := range frames {
		if err := os.Remove(f.path); err != nil && !errors.Is(err, fs.ErrNotExist) {
			c.log.Warn("evicting a cached frame failed", "object", f.key.object, "frame", f.key.frame, "error", err)
			c.mu.Lock()
			c.linkLocked(f, false)
			c.mu.Unlock()
		}
	}
}

// linkLocked counts f and lists it as the most recently used frame, or the
// least. It is found by key unless another copy is.
func (c *frameCache) linkLocked(f *cachedFrame, recent bool) {
	f.held = c.holds[f.key] > 0
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
