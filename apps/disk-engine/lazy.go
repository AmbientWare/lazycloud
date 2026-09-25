package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"slices"
	"sort"
	"sync"
	"time"

	"golang.org/x/sys/unix"
)

// chunkSource reads one stored object. objectStore is the production one.
type chunkSource interface {
	get(ctx context.Context, key string, limit int64) ([]byte, error)
}

const (
	fetchAttempts     = 6
	fetchFirstBackoff = 500 * time.Millisecond
	fetchMaxBackoff   = 8 * time.Second
	// fetchTimeout bounds one GET of one chunk, 8 MiB at most.
	fetchTimeout = 20 * time.Second
)

// lazyLayer is a published layer file whose chunks arrive on first read. The
// file has the layer's full size from the start; ranges no chunk covers are
// holes, which read as the zeroes the manifest left out. present has one bit
// per chunk, in manifest order, set once that chunk's verified bytes are in
// the file.
type lazyLayer struct {
	diskID   string
	name     string
	manifest layerManifest
	file     *os.File
	bitmap   *os.File
	source   chunkSource
	backoff  time.Duration
	// life ends every fetch when the layer stops being served. A fetch runs
	// on it rather than on its reader, so the others waiting on the same
	// chunk still get it when that reader gives up.
	life context.Context
	// touched receives the digest of every chunk a read covers.
	touched func(sum string)

	downloads sync.WaitGroup

	mu       sync.Mutex
	present  []byte
	count    int
	dirty    uint64
	flushed  uint64
	inflight map[int]*fetchCall
}

type fetchCall struct {
	done chan struct{}
	err  error
}

func manifestPath(p diskPaths, l layer) string { return p.layerPath(l) + ".manifest" }
func bitmapPath(p diskPaths, l layer) string   { return p.layerPath(l) + ".present" }

func bitmapBytes(chunks int) int { return (chunks + 7) / 8 }

func bitSet(bits []byte, i int) bool { return bits[i/8]&(1<<(i%8)) != 0 }

// createLazyLayer lays out a layer to be filled on demand: its manifest beside
// it, a sparse file of the layer's size, and an empty bitmap.
func createLazyLayer(p diskPaths, l layer, manifest layerManifest) error {
	data, err := json.Marshal(manifest)
	if err != nil {
		return err
	}
	if err := writeFileAtomic(manifestPath(p, l), data); err != nil {
		return err
	}
	if err := writeFileAtomic(bitmapPath(p, l), make([]byte, bitmapBytes(len(manifest.Chunks)))); err != nil {
		return err
	}
	file, err := os.OpenFile(p.layerPath(l), os.O_RDWR|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	defer file.Close()
	if err := file.Truncate(manifest.LayerSizeBytes); err != nil {
		return err
	}
	return file.Sync()
}

// readLazyRecord reads a lazy layer's manifest, chunks in file order, and the
// bitmap on disk.
func readLazyRecord(p diskPaths, l layer) (layerManifest, []byte, error) {
	var manifest layerManifest
	if err := readJSONFile(manifestPath(p, l), &manifest); err != nil {
		return manifest, nil, err
	}
	sort.Slice(manifest.Chunks, func(i, j int) bool { return manifest.Chunks[i].Offset < manifest.Chunks[j].Offset })
	present, err := os.ReadFile(bitmapPath(p, l))
	if err != nil {
		return manifest, nil, err
	}
	if len(present) != bitmapBytes(len(manifest.Chunks)) {
		return manifest, nil, fmt.Errorf("%s holds %d bytes for %d chunks", bitmapPath(p, l), len(present), len(manifest.Chunks))
	}
	return manifest, present, nil
}

// lazyOwed is what a lazy layer still has to fetch, as the space it will take,
// and whether it has fetched everything.
func lazyOwed(p diskPaths, l layer) (int64, bool, error) {
	manifest, present, err := readLazyRecord(p, l)
	if err != nil {
		return 0, false, err
	}
	var owed int64
	complete := true
	for i, chunk := range manifest.Chunks {
		if !bitSet(present, i) {
			owed += blockRounded(chunk.Length)
			complete = false
		}
	}
	return owed, complete, nil
}

func openLazyLayer(ctx context.Context, p diskPaths, l layer, source chunkSource) (*lazyLayer, error) {
	manifest, present, err := readLazyRecord(p, l)
	if err != nil {
		return nil, err
	}
	file, err := os.OpenFile(p.layerPath(l), os.O_RDWR, 0)
	if err != nil {
		return nil, err
	}
	bitmap, err := os.OpenFile(bitmapPath(p, l), os.O_RDWR, 0)
	if err != nil {
		file.Close()
		return nil, err
	}
	layer := &lazyLayer{
		diskID: p.id, name: l.file(), manifest: manifest, file: file, bitmap: bitmap,
		source: source, backoff: fetchFirstBackoff, life: ctx, present: present,
		inflight: map[int]*fetchCall{},
	}
	for i := range manifest.Chunks {
		if bitSet(present, i) {
			layer.count++
		}
	}
	return layer, nil
}

// close waits out downloads, which end at once when life has, then flushes.
func (l *lazyLayer) close() error {
	l.downloads.Wait()
	return errors.Join(l.flush(), l.file.Close(), l.bitmap.Close())
}

func (l *lazyLayer) has(i int) bool { return bitSet(l.present, i) }

func (l *lazyLayer) complete() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.count == len(l.manifest.Chunks)
}

func (l *lazyLayer) Size() int64 { return l.manifest.LayerSizeBytes }

// ReadAt serves a read of the layer, fetching every chunk it touches first.
func (l *lazyLayer) ReadAt(ctx context.Context, buf []byte, offset int64) error {
	if err := l.ensure(ctx, offset, int64(len(buf))); err != nil {
		return err
	}
	_, err := l.file.ReadAt(buf, offset)
	return err
}

// ensure makes every chunk under [offset, offset+length) present.
func (l *lazyLayer) ensure(ctx context.Context, offset, length int64) error {
	chunks := l.manifest.Chunks
	first := sort.Search(len(chunks), func(i int) bool { return chunks[i].Offset+chunks[i].Length > offset })
	var errs []error
	for i := first; i < len(chunks) && chunks[i].Offset < offset+length; i++ {
		if l.touched != nil {
			l.touched(chunks[i].SHA256)
		}
		errs = append(errs, l.fetch(ctx, i))
	}
	return errors.Join(errs...)
}

// fetch makes chunk i present. Concurrent callers share one download.
func (l *lazyLayer) fetch(ctx context.Context, i int) error {
	l.mu.Lock()
	if l.has(i) {
		l.mu.Unlock()
		return nil
	}
	call, running := l.inflight[i]
	if !running {
		call = &fetchCall{done: make(chan struct{})}
		l.inflight[i] = call
		l.downloads.Add(1)
		go l.download(i, call)
	}
	l.mu.Unlock()
	select {
	case <-call.done:
		return call.err
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (l *lazyLayer) download(i int, call *fetchCall) {
	defer l.downloads.Done()
	call.err = l.fetchVerified(i)
	l.mu.Lock()
	delete(l.inflight, i)
	if call.err == nil {
		l.present[i/8] |= 1 << (i % 8)
		l.count++
		l.dirty++
	}
	l.mu.Unlock()
	close(call.done)
}

// fetchVerified downloads chunk i, checks it against the manifest and writes
// it into the file. Bytes that fail the check are never written.
func (l *lazyLayer) fetchVerified(i int) error {
	chunk := l.manifest.Chunks[i]
	key := diskChunkKey(l.diskID, chunk.SHA256)
	wait := l.backoff
	var last error
	for attempt := range fetchAttempts {
		if attempt > 0 {
			select {
			case <-time.After(wait):
			case <-l.life.Done():
				return errors.Join(last, l.life.Err())
			}
			wait = min(wait*2, fetchMaxBackoff)
		}
		ctx, cancel := context.WithTimeout(l.life, fetchTimeout)
		data, err := l.source.get(ctx, key, chunk.Length)
		cancel()
		if err != nil {
			last = err
			continue
		}
		if sum := sha256.Sum256(data); int64(len(data)) != chunk.Length || hex.EncodeToString(sum[:]) != chunk.SHA256 {
			last = fmt.Errorf("chunk %s does not match its digest or length", key)
			continue
		}
		if _, err := l.file.WriteAt(data, chunk.Offset); err != nil {
			return fmt.Errorf("write chunk %s into %s: %w", key, l.name, err)
		}
		return nil
	}
	return fmt.Errorf("fetch chunk %s of %s after %d attempts: %w", key, l.name, fetchAttempts, last)
}

// flush makes the bitmap durable. The layer file is flushed first, so a bit on
// disk always names bytes on disk; a crash can only forget chunks, which are
// then fetched again.
func (l *lazyLayer) flush() error {
	l.mu.Lock()
	if l.dirty == l.flushed {
		l.mu.Unlock()
		return nil
	}
	target := l.dirty
	bits := slices.Clone(l.present)
	l.mu.Unlock()
	if err := unix.Fdatasync(int(l.file.Fd())); err != nil {
		return fmt.Errorf("flush %s: %w", l.name, err)
	}
	if _, err := l.bitmap.WriteAt(bits, 0); err != nil {
		return err
	}
	if err := unix.Fdatasync(int(l.bitmap.Fd())); err != nil {
		return err
	}
	l.mu.Lock()
	l.flushed = max(l.flushed, target)
	l.mu.Unlock()
	return nil
}

// progress reports how much of the layer is present.
func (l *lazyLayer) progress() (chunks, present int, bytes, presentBytes int64) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for i, chunk := range l.manifest.Chunks {
		bytes += chunk.Length
		if l.has(i) {
			presentBytes += chunk.Length
		}
	}
	return len(l.manifest.Chunks), l.count, bytes, presentBytes
}
