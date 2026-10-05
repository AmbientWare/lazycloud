package snapshotter

import (
	"container/list"
	"context"
	"errors"
	"fmt"
	"hash/fnv"
	"io"
	"io/fs"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/singleflight"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
)

const (
	// fetchAttempts bounds the reads of one frame from the store; the
	// backoff runs between them.
	fetchAttempts = 3
	// fetchTimeout bounds all attempts at one frame together.
	fetchTimeout = 2 * time.Minute
	// minCacheBytes keeps room for the frames concurrent reads hold.
	minCacheBytes = 64 << 20
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
}

// frameCache keeps uncompressed frames on local disk under a byte bound,
// evicting the least recently used first and frames of mounted layers last.
// The store is its durable source: it starts empty and refills on reads.
type frameCache struct {
	dir     string
	limit   int64
	grants  *grants
	metrics *metrics
	log     *slog.Logger
	// fetches makes concurrent reads of one frame share one fetch, and
	// slots bounds the fetches in flight and so the memory they hold.
	fetches singleflight.Group
	slots   chan struct{}
	// filling bounds the background fetches in flight, of mounted layers
	// and prefetches. They hold at most half of slots, so containers' reads
	// always find a slot free.
	filling chan struct{}
	// traces records the frames read through mounts for the agent.
	traces *tracer
	// prefetching bounds the prefetches running, and background waits for
	// them.
	prefetching chan struct{}
	background  sync.WaitGroup
	// life bounds every fetch: a fetch is shared, so no one reader's or
	// fill's context may end it.
	life context.Context
	// keys serialises writing and evicting the file of one frame, so an
	// eviction never deletes a frame stored again since.
	keys [64]sync.Mutex

	mu      sync.Mutex
	used    int64
	lru     *list.List // of *cachedFrame, most recent first
	frames  map[frameKey]*list.Element
	mounted map[imagefs.Digest]int
	// live holds a mounted layer of each digest mounted, and mountWake
	// closes when another mounts.
	live      map[imagefs.Digest]*layer
	mountWake chan struct{}
}

func newFrameCache(life context.Context, dir string, limit int64, fetches int, g *grants, m *metrics, log *slog.Logger) (*frameCache, error) {
	if limit < minCacheBytes {
		return nil, fmt.Errorf("frame cache bound %d is under %d bytes", limit, minCacheBytes)
	}
	if fetches < 1 {
		return nil, fmt.Errorf("frame fetch concurrency %d is under 1", fetches)
	}
	if err := os.RemoveAll(dir); err != nil {
		return nil, fmt.Errorf("clear frame cache: %w", err)
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, fmt.Errorf("create frame cache: %w", err)
	}
	return &frameCache{
		dir: dir, limit: limit, grants: g, metrics: m, log: log,
		slots:       make(chan struct{}, fetches),
		filling:     make(chan struct{}, max(1, fetches/2)),
		traces:      newTracer(time.Now),
		prefetching: make(chan struct{}, maxPrefetches),
		life:        life,
		lru:         list.New(),
		frames:      make(map[frameKey]*list.Element),
		mounted:     make(map[imagefs.Digest]int),
		live:        make(map[imagefs.Digest]*layer),
		mountWake:   make(chan struct{}),
	}, nil
}

func (c *frameCache) path(k frameKey) string {
	return filepath.Join(c.dir, strings.TrimPrefix(string(k.layer), "sha256:"), strconv.Itoa(k.frame))
}

// read fills p with frame's bytes from off.
func (c *frameCache) read(l *layer, frame int, p []byte, off int64) error {
	c.traces.record(l.digest, frame)
	k := frameKey{layer: l.digest, frame: frame}
	// A frame evicted since the lookup, or unreadable, is fetched again.
	if c.touch(k) && readAt(c.path(k), p, off) == nil {
		c.metrics.cacheHits.Inc()
		return nil
	}
	data, err := c.load(l, frame) //nolint:contextcheck // a shared fetch runs under the cache's life
	if err != nil {
		return err
	}
	if off+int64(len(p)) > int64(len(data)) {
		return fmt.Errorf("%w: read past frame %d of layer %s", imagefs.ErrInvalidIndex, frame, l.digest)
	}
	copy(p, data[off:])
	return nil
}

// fill fetches every frame of l the cache lacks, a few at a time and
// sharing fetches with reads. Once ctx ends it starts no more; the fetches
// under way finish, since reads may be waiting on them. A cold read
// otherwise waits one store round trip per frame it touches.
func (c *frameCache) fill(ctx context.Context, l *layer) {
	var wg sync.WaitGroup
	defer wg.Wait()
	for i := range l.index.Frames {
		c.mu.Lock()
		_, cached := c.frames[frameKey{layer: l.digest, frame: i}]
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
			if _, err := c.load(l, i); err != nil {
				c.log.Debug("background fetch failed", "layer", l.digest, "frame", i, "error", err)
			}
		})
	}
}

// load returns frame's bytes, fetching it once however many reads wait.
// The fetch runs under the cache's life and fetchTimeout alone.
func (c *frameCache) load(l *layer, frame int) ([]byte, error) {
	k := frameKey{layer: l.digest, frame: frame}
	v, err, _ := c.fetches.Do(string(l.digest)+"/"+strconv.Itoa(frame), func() (any, error) {
		// A fetch that just finished may have stored it.
		frameLen := min(imagefs.FrameSize, l.index.StreamSize-int64(frame)*imagefs.FrameSize)
		if c.touch(k) {
			if data, err := os.ReadFile(c.path(k)); err == nil && int64(len(data)) == frameLen {
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
		data, err := c.fetch(ctx, l, frame)
		if err != nil {
			return nil, err
		}
		c.metrics.framesFetched.Inc()
		c.metrics.bytesFetched.Add(float64(l.index.Frames[frame].Size))
		if err := c.store(k, data); err != nil {
			c.metrics.storeFailures.Inc()
			c.log.WarnContext(ctx, "frame cache write failed", "layer", l.digest, "frame", frame, "error", err)
		}
		return data, nil
	})
	if err != nil {
		return nil, err //nolint:wrapcheck // fetch wraps with the layer and frame
	}
	return v.([]byte), nil //nolint:forcetypeassert // the function returns []byte
}

// fetch reads one frame from the store with bounded retries, taking the
// layer's grant at each attempt so a refreshed URL applies at once.
func (c *frameCache) fetch(ctx context.Context, l *layer, frame int) ([]byte, error) {
	var err error
	for attempt := range fetchAttempts {
		if attempt > 0 {
			if serr := sleep(ctx, backoff(attempt)); serr != nil {
				break
			}
		}
		if _, ok := c.grants.lookup(l.digest); !ok {
			return nil, fmt.Errorf("read frame %d of layer %s: %w", frame, l.digest, errNoGrant)
		}
		var data []byte
		data, err = l.index.ReadFrame(ctx, l.data, frame)
		if err == nil {
			return data, nil
		}
		if !retryable(err) {
			break
		}
	}
	return nil, fmt.Errorf("read frame %d of layer %s: %w", frame, l.digest, err)
}

// retryable reports whether another attempt may succeed: a transport
// error, a store error, or a refused URL the agent may have refreshed.
func retryable(err error) bool {
	if errors.Is(err, imagefs.ErrInvalidIndex) || errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return false
	}
	var status *imagefs.StatusError
	if errors.As(err, &status) {
		return status.StatusCode >= 500 || status.StatusCode == 429 || status.StatusCode == 403 || status.StatusCode == 400
	}
	return true
}

// store writes a fetched frame and evicts down to the bound. The frame's
// bytes reach their readers whether or not it is stored.
func (c *frameCache) store(k frameKey, data []byte) error {
	if err := c.write(k, data); err != nil {
		return err
	}
	c.mu.Lock()
	victims := c.evictLocked()
	used := c.used
	c.mu.Unlock()
	c.metrics.cacheBytes.Set(float64(used))
	var errs []error
	for _, v := range victims {
		if err := c.remove(v); err != nil {
			errs = append(errs, err)
		}
		c.metrics.evictions.Inc()
	}
	return errors.Join(errs...)
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
		c.lru.MoveToFront(e)
	} else {
		c.frames[k] = c.lru.PushFront(&cachedFrame{key: k, size: int64(len(data))})
		c.used += int64(len(data))
	}
	return nil
}

// remove deletes an evicted frame's file unless it was stored again since.
func (c *frameCache) remove(k frameKey) error {
	lock := c.keyLock(k)
	lock.Lock()
	defer lock.Unlock()
	c.mu.Lock()
	_, back := c.frames[k]
	c.mu.Unlock()
	if back {
		return nil
	}
	if err := os.Remove(c.path(k)); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return fmt.Errorf("evict frame: %w", err)
	}
	return nil
}

// evictLocked drops frames until the cache fits its bound: unmounted
// layers' frames first, then mounted layers', least recently used first.
func (c *frameCache) evictLocked() []frameKey {
	var victims []frameKey
	for _, mountedToo := range []bool{false, true} {
		for e := c.lru.Back(); e != nil && c.used > c.limit; {
			prev := e.Prev()
			f := e.Value.(*cachedFrame) //nolint:forcetypeassert // the list holds *cachedFrame
			if mountedToo || c.mounted[f.key.layer] == 0 {
				c.lru.Remove(e)
				delete(c.frames, f.key)
				c.used -= f.size
				victims = append(victims, f.key)
			}
			e = prev
		}
	}
	return victims
}

// touch marks k used and reports whether it is cached.
func (c *frameCache) touch(k frameKey) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	e, ok := c.frames[k]
	if ok {
		c.lru.MoveToFront(e)
	}
	return ok
}

// setMounted counts a mount of l up or down.
func (c *frameCache) setMounted(l *layer, delta int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.mounted[l.digest] += delta
	if c.mounted[l.digest] <= 0 {
		delete(c.mounted, l.digest)
		delete(c.live, l.digest)
		return
	}
	if _, ok := c.live[l.digest]; !ok {
		c.live[l.digest] = l
		close(c.mountWake)
		c.mountWake = make(chan struct{})
	}
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
