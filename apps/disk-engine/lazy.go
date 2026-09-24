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

	mu       sync.Mutex
	present  []byte
	count    int
	dirty    uint64
	flushed  uint64
	inflight map[int]*fetchCall
	// touched receives the digest of every chunk a read covers, for the heat map.
	touched func(sum string)
}

type fetchCall struct {
	done chan struct{}
	err  error
}

func manifestPath(p diskPaths, l layer) string { return p.layerPath(l) + ".manifest" }
func bitmapPath(p diskPaths, l layer) string   { return p.layerPath(l) + ".present" }

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

func bitmapBytes(chunks int) int { return (chunks + 7) / 8 }

func openLazyLayer(p diskPaths, l layer, source chunkSource) (*lazyLayer, error) {
	var manifest layerManifest
	if err := readJSONFile(manifestPath(p, l), &manifest); err != nil {
		return nil, err
	}
	sort.Slice(manifest.Chunks, func(i, j int) bool { return manifest.Chunks[i].Offset < manifest.Chunks[j].Offset })
	present, err := os.ReadFile(bitmapPath(p, l))
	if err != nil {
		return nil, err
	}
	if len(present) != bitmapBytes(len(manifest.Chunks)) {
		return nil, fmt.Errorf("%s holds %d bytes for %d chunks", bitmapPath(p, l), len(present), len(manifest.Chunks))
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
		source: source, backoff: fetchFirstBackoff, present: present, inflight: map[int]*fetchCall{},
	}
	for i := range manifest.Chunks {
		if layer.has(i) {
			layer.count++
		}
	}
	return layer, nil
}

func (l *lazyLayer) close() error {
	return errors.Join(l.flush(), l.file.Close(), l.bitmap.Close())
}

func (l *lazyLayer) has(i int) bool { return l.present[i/8]&(1<<(i%8)) != 0 }

func (l *lazyLayer) complete() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.count == len(l.manifest.Chunks)
}

// overlapping lists the chunks that hold bytes in [offset, offset+length).
func (l *lazyLayer) overlapping(offset, length int64) []int {
	chunks := l.manifest.Chunks
	first := sort.Search(len(chunks), func(i int) bool { return chunks[i].Offset+chunks[i].Length > offset })
	var found []int
	for i := first; i < len(chunks) && chunks[i].Offset < offset+length; i++ {
		found = append(found, i)
	}
	return found
}

// ensure makes every chunk under [offset, offset+length) present, fetching
// the missing ones. Concurrent callers share one fetch of each chunk.
func (l *lazyLayer) ensure(ctx context.Context, offset, length int64) error {
	var errs []error
	for _, i := range l.overlapping(offset, length) {
		if l.touched != nil {
			l.touched(l.manifest.Chunks[i].SHA256)
		}
		if err := l.fetch(ctx, i); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

func (l *lazyLayer) fetch(ctx context.Context, i int) error {
	l.mu.Lock()
	if l.has(i) {
		l.mu.Unlock()
		return nil
	}
	if call, running := l.inflight[i]; running {
		l.mu.Unlock()
		select {
		case <-call.done:
			return call.err
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	call := &fetchCall{done: make(chan struct{})}
	l.inflight[i] = call
	l.mu.Unlock()

	// The fetch outlives a caller that gives up, so the others waiting on it
	// still get the chunk.
	call.err = l.download(context.WithoutCancel(ctx), i)
	l.mu.Lock()
	delete(l.inflight, i)
	if call.err == nil {
		l.present[i/8] |= 1 << (i % 8)
		l.count++
		l.dirty++
	}
	l.mu.Unlock()
	close(call.done)
	return call.err
}

// download fetches chunk i, checks it against the manifest, and writes it
// into the file. Bytes that fail the check are never written.
func (l *lazyLayer) download(ctx context.Context, i int) error {
	chunk := l.manifest.Chunks[i]
	key := diskChunkKey(l.diskID, chunk.SHA256)
	wait := l.backoff
	var last error
	for attempt := range fetchAttempts {
		if attempt > 0 {
			select {
			case <-time.After(wait):
			case <-ctx.Done():
				return errors.Join(last, ctx.Err())
			}
			wait = min(wait*2, fetchMaxBackoff)
		}
		data, err := l.source.get(ctx, key, chunk.Length)
		if err != nil {
			last = err
			continue
		}
		sum := sha256.Sum256(data)
		if int64(len(data)) != chunk.Length || hex.EncodeToString(sum[:]) != chunk.SHA256 {
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
// disk always names bytes that are on disk too; a crash only forgets chunks,
// which are then fetched again.
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

// ReadAt serves a read of the layer, fetching what it touches first.
func (l *lazyLayer) ReadAt(ctx context.Context, buf []byte, offset int64) error {
	if err := l.ensure(ctx, offset, int64(len(buf))); err != nil {
		return err
	}
	_, err := l.file.ReadAt(buf, offset)
	return err
}

func (l *lazyLayer) Size() int64 { return l.manifest.LayerSizeBytes }

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

// lazyComplete reports, from the bitmap on disk, whether every chunk of a lazy
// layer is present.
func lazyComplete(p diskPaths, l layer) (bool, error) {
	var manifest layerManifest
	if err := readJSONFile(manifestPath(p, l), &manifest); err != nil {
		return false, err
	}
	present, err := os.ReadFile(bitmapPath(p, l))
	if err != nil {
		return false, err
	}
	for i := range manifest.Chunks {
		if i/8 >= len(present) || present[i/8]&(1<<(i%8)) == 0 {
			return false, nil
		}
	}
	return true, nil
}
