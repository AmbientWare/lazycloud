package main

import (
	"bytes"
	"context"
	"encoding/hex"
	"errors"
	"math/rand/v2"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"sync"
	"testing"
	"time"
)

// memorySource is a bucket of chunks by key, counting every read.
type memorySource struct {
	mu      sync.Mutex
	objects map[string][]byte
	gets    map[string]int
	delay   time.Duration
}

func (m *memorySource) get(ctx context.Context, key string, limit int64) ([]byte, error) {
	time.Sleep(m.delay)
	m.mu.Lock()
	defer m.mu.Unlock()
	m.gets[key]++
	data, ok := m.objects[key]
	if !ok {
		return nil, errors.New("no such key")
	}
	return slices.Clone(data), nil
}

// lazyFixture stores data's non-zero chunks and lays out a lazy layer for it.
func lazyFixture(t *testing.T, data []byte, format string) (diskPaths, layer, *memorySource) {
	t.Helper()
	p := diskPaths{root: t.TempDir(), id: "disk-1"}
	if err := os.MkdirAll(p.layerDir(), 0o700); err != nil {
		t.Fatal(err)
	}
	source := &memorySource{objects: map[string][]byte{}, gets: map[string]int{}}
	manifest := layerManifest{DiskID: p.id, Generation: 1, VirtualSizeBytes: int64(len(data)), LayerSizeBytes: int64(len(data)), Format: format}
	for _, span := range chunkBytes(t, data) {
		if span.Zero {
			continue
		}
		sum := hex.EncodeToString(span.Sum[:])
		manifest.Chunks = append(manifest.Chunks, manifestChunk{Offset: span.Offset, Length: span.Length, SHA256: sum})
		source.objects[diskChunkKey(p.id, sum)] = data[span.Offset : span.Offset+span.Length]
	}
	l := layer{Seq: 1, Generation: 1, Lazy: true, Raw: format == formatRaw}
	if err := createLazyLayer(p, l, manifest); err != nil {
		t.Fatal(err)
	}
	return p, l, source
}

func randomBytes(size int, seed byte) []byte {
	data := make([]byte, size)
	rand.NewChaCha8([32]byte{seed}).Read(data)
	return data
}

func openFixture(t *testing.T, p diskPaths, l layer, source chunkSource) *lazyLayer {
	t.Helper()
	lazy, err := openLazyLayer(context.Background(), p, l, source)
	if err != nil {
		t.Fatal(err)
	}
	lazy.backoff = time.Millisecond
	return lazy
}

// Many reads of a cold region at once must cost one fetch per chunk, and a
// read after that none, or a busy disk multiplies its bucket traffic.
func TestConcurrentReadsFetchEachChunkOnce(t *testing.T) {
	data := randomBytes(24<<20, 1)
	p, l, source := lazyFixture(t, data, formatRaw)
	source.delay = 20 * time.Millisecond
	lazy := openFixture(t, p, l, source)
	defer lazy.close()

	var wg sync.WaitGroup
	failures := make(chan error, 32)
	for i := range 32 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			offset := int64(i%8) * (3 << 20)
			buf := make([]byte, 2<<20)
			if err := lazy.ReadAt(context.Background(), buf, offset); err != nil {
				failures <- err
				return
			}
			if !bytes.Equal(buf, data[offset:offset+int64(len(buf))]) {
				failures <- errors.New("read returned the wrong bytes")
			}
		}()
	}
	wg.Wait()
	close(failures)
	for err := range failures {
		t.Fatal(err)
	}
	for key, gets := range source.gets {
		if gets != 1 {
			t.Fatalf("%s fetched %d times", key, gets)
		}
	}
	before := len(source.gets)
	if err := lazy.ReadAt(context.Background(), make([]byte, 4096), 0); err != nil {
		t.Fatal(err)
	}
	if len(source.gets) != before || source.gets[diskChunkKey(p.id, lazy.manifest.Chunks[0].SHA256)] != 1 {
		t.Fatal("a read of a fetched chunk went back to the bucket")
	}
}

// A chunk whose bytes do not match the manifest must never reach the layer
// file, however often it is fetched; the read fails instead.
func TestACorruptChunkIsNeverWritten(t *testing.T) {
	data := randomBytes(4<<20, 2)
	p, l, source := lazyFixture(t, data, formatRaw)
	for key, stored := range source.objects {
		corrupt := slices.Clone(stored)
		corrupt[0] ^= 0xff
		source.objects[key] = corrupt
	}
	lazy := openFixture(t, p, l, source)
	defer lazy.close()
	first := diskChunkKey(p.id, lazy.manifest.Chunks[0].SHA256)

	if err := lazy.ReadAt(context.Background(), make([]byte, 4096), 0); err == nil {
		t.Fatal("a read of a corrupt chunk succeeded")
	}
	if source.gets[first] != fetchAttempts {
		t.Fatalf("corrupt chunk fetched %d times, want %d", source.gets[first], fetchAttempts)
	}
	if lazy.has(0) {
		t.Fatal("a corrupt chunk was marked present")
	}
	written, err := os.ReadFile(p.layerPath(l))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(written, make([]byte, len(written))) {
		t.Fatal("corrupt bytes were written into the layer")
	}
}

// The bitmap is the only thing that says a chunk is present. A crash before it
// is flushed, or halfway through writing a chunk, must leave the chunk to be
// fetched again rather than served from a partial write.
func TestACrashMidFetchNeverLeavesAPartialChunk(t *testing.T) {
	data := randomBytes(8<<20, 3)
	p, l, source := lazyFixture(t, data, formatRaw)
	chunk := func(lazy *lazyLayer) manifestChunk { return lazy.manifest.Chunks[0] }

	// A crash halfway through writing the first chunk.
	lazy := openFixture(t, p, l, source)
	c := chunk(lazy)
	if _, err := lazy.file.WriteAt(data[c.Offset:c.Offset+c.Length/2], c.Offset); err != nil {
		t.Fatal(err)
	}
	lazy.file.Close()
	lazy.bitmap.Close()

	// A fetch that completed but whose bit never reached the disk.
	lazy = openFixture(t, p, l, source)
	if lazy.has(0) {
		t.Fatal("a half-written chunk is marked present")
	}
	if err := lazy.fetch(context.Background(), 0); err != nil {
		t.Fatal(err)
	}
	lazy.file.Close()
	lazy.bitmap.Close()

	lazy = openFixture(t, p, l, source)
	if lazy.has(0) {
		t.Fatal("a chunk is marked present before its bit was flushed")
	}
	buf := make([]byte, c.Length)
	if err := lazy.ReadAt(context.Background(), buf, c.Offset); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(buf, data[c.Offset:c.Offset+c.Length]) {
		t.Fatal("the refetched chunk does not hold its bytes")
	}
	if err := lazy.close(); err != nil {
		t.Fatal(err)
	}
	lazy = openFixture(t, p, l, source)
	defer lazy.close()
	if !lazy.has(0) {
		t.Fatal("a flushed chunk was forgotten")
	}
}

// qemu opens a lazy layer through `serve` as a backing file's protocol node.
// Reading a qcow2 image and a raw one through the NBD server must return the
// bytes qemu reads from the complete files.
func TestQemuReadsLazyLayersOverNBD(t *testing.T) {
	if _, err := exec.LookPath(toolImage); err != nil {
		t.Skip("qemu-img is not installed")
	}
	dir := t.TempDir()
	image := filepath.Join(dir, "source.qcow2")
	run := func(name string, args ...string) {
		t.Helper()
		if out, err := exec.Command(name, args...).CombinedOutput(); err != nil {
			t.Fatalf("%s %v: %v: %s", name, args, err, out)
		}
	}
	run(toolImage, "create", "-q", "-f", "qcow2", image, "64M")
	run("qemu-io", "-f", "qcow2", "-c", "write -P 0xab 1M 5M", "-c", "write -P 0xcd 40M 3M", image)
	qcow2Bytes, err := os.ReadFile(image)
	if err != nil {
		t.Fatal(err)
	}

	cases := []struct {
		name   string
		format string
		data   []byte
	}{
		{"qcow2", formatQcow2, qcow2Bytes},
		{"raw", formatRaw, randomBytes(12<<20, 4)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			p, l, source := lazyFixture(t, tc.data, tc.format)
			lazy := openFixture(t, p, l, source)
			defer lazy.close()
			socket := filepath.Join(t.TempDir(), "lazy.sock")
			listener, err := net.Listen("unix", socket)
			if err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			server := &nbdServer{exports: map[string]nbdExport{l.file(): lazy}}
			go server.serve(ctx, listener)

			want := filepath.Join(t.TempDir(), "want.raw")
			got := filepath.Join(t.TempDir(), "got.raw")
			complete := filepath.Join(t.TempDir(), "complete."+tc.format)
			if err := os.WriteFile(complete, tc.data, 0o600); err != nil {
				t.Fatal(err)
			}
			run(toolImage, "convert", "-f", tc.format, "-O", "raw", complete, want)
			spec := `json:{"driver":"` + tc.format + `","file":{"driver":"nbd","server":{"type":"unix","path":"` + socket + `"},"export":"` + l.file() + `"}}`
			run(toolImage, "convert", "-O", "raw", spec, got)
			wantBytes, _ := os.ReadFile(want)
			gotBytes, _ := os.ReadFile(got)
			if !bytes.Equal(wantBytes, gotBytes) {
				t.Fatal("the image read through the NBD server differs from the complete file")
			}
		})
	}
}
