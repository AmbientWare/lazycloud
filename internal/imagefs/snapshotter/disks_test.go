package snapshotter

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"golang.org/x/sys/unix"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// storedDisk is generation 1 of a disk in the test store: frame i holds
// frames[i], or zeros where it is nil.
type storedDisk struct {
	id    string
	index imagefs.DiskIndex
	sum   string
}

// serveRequest serves the disk's generation, its index kept at a path of
// the test's.
func (sd storedDisk) serveRequest(t *testing.T, prefetch bool) *imagefsproto.ServeDiskRequest {
	t.Helper()
	return &imagefsproto.ServeDiskRequest{DiskId: sd.id, Generation: 1, IndexSha256: sd.sum, IndexPath: filepath.Join(t.TempDir(), "index"), Prefetch: prefetch}
}

func (ts *testStore) disk(t *testing.T, id string, frames [][]byte, start []uint32) storedDisk {
	t.Helper()
	enc, err := imagefs.NewDiskFrameEncoder(1)
	if err != nil {
		t.Fatal(err)
	}
	ix := imagefs.DiskIndex{Size: int64(len(frames)) * imagefs.FrameSize, Start: start}
	for _, data := range frames {
		if data == nil {
			data = make([]byte, imagefs.FrameSize)
		}
		f, packed := enc.Encode(data)
		ix.Frames = append(ix.Frames, f)
		if packed != nil {
			ts.putRoot(t, imagefs.DiskPrefix(id)+f.Name(), packed)
		}
	}
	raw, err := ix.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	ts.putRoot(t, imagefs.DiskIndexKey(id, 1, hex.EncodeToString(sum[:])), raw)
	return storedDisk{id: id, index: ix, sum: hex.EncodeToString(sum[:])}
}

// putRoot stores body at key outside the store's prefix, where disks are:
// the test's bucket is its own.
func (ts *testStore) putRoot(t *testing.T, key string, body []byte) {
	t.Helper()
	if _, err := ts.client.PutObject(t.Context(), &s3.PutObjectInput{Bucket: aws.String(ts.bucket), Key: aws.String(key), Body: bytes.NewReader(body)}); err != nil {
		t.Fatal(err)
	}
}

// serveDisks mounts a disk directory over a cache of bound bytes.
func serveDisks(t *testing.T, transport http.RoundTripper) *disks {
	t.Helper()
	if os.Geteuid() != 0 {
		t.Fatal("the snapshotter tests mount FUSE filesystems and run as root")
	}
	c := newTestCache(t, transport, 64*imagefs.FrameSize)
	d := newDisks(t.TempDir(), c, &http.Client{Transport: transport}, slog.New(slog.DiscardHandler))
	t.Cleanup(func() {
		if d.server != nil {
			if err := d.server.Unmount(); err != nil {
				t.Error(err)
			}
		}
	})
	return d
}

func (d *disks) grantTest(t *testing.T, ts *testStore, id string) {
	t.Helper()
	cfg := storagetest.Config(t)
	if err := d.grant(id, &imagefsproto.DiskGrant{
		Endpoint: cfg.Endpoint, Region: cfg.Region, Bucket: ts.bucket, ForcePathStyle: true,
		AccessKeyId: cfg.AccessKeyID, SecretAccessKey: cfg.SecretAccessKey, ExpiresAt: timestamppb.New(time.Now().Add(time.Hour)),
	}); err != nil {
		t.Fatal(err)
	}
}

func readFile(t *testing.T, path string, off int64, n int) []byte {
	t.Helper()
	f, err := os.Open(path) //nolint:gosec // A test file.
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()
	p := make([]byte, n)
	if _, err := f.ReadAt(p, off); err != nil {
		t.Fatal(err)
	}
	return p
}

// A served generation fetches a frame on its first read and no other: a
// frame of zeros is a hole that reads without a fetch, and a cached frame
// reads again without one.
func TestDiskReadsFetchOnlyTheTouchedFrames(t *testing.T) {
	ts := newTestStore(t)
	frames := [][]byte{bytes.Repeat([]byte{1}, imagefs.FrameSize), nil, bytes.Repeat([]byte{2}, imagefs.FrameSize), bytes.Repeat([]byte{3}, imagefs.FrameSize/2)}
	frames[3] = append(frames[3], make([]byte, imagefs.FrameSize/2)...)
	stored := ts.disk(t, "d1", frames, nil)
	counting := &countingTransport{}
	d := serveDisks(t, counting)
	d.grantTest(t, ts, stored.id)
	g, err := d.serve(t.Context(), stored.serveRequest(t, false))
	if err != nil {
		t.Fatal(err)
	}
	path := d.path(g)
	fetched := counting.requests.Load()

	if got := readFile(t, path, 2*imagefs.FrameSize+100, 10); !bytes.Equal(got, bytes.Repeat([]byte{2}, 10)) {
		t.Fatalf("frame 2 read %v", got)
	}
	if n := counting.requests.Load() - fetched; n != 1 {
		t.Fatalf("one read of frame 2 sent %d requests", n)
	}
	if got := readFile(t, path, imagefs.FrameSize, 4096); !bytes.Equal(got, make([]byte, 4096)) {
		t.Fatal("a frame of zeros read non-zero bytes")
	}
	readFile(t, path, 2*imagefs.FrameSize, imagefs.FrameSize)
	if n := counting.requests.Load() - fetched; n != 1 {
		t.Fatalf("reading a hole and a cached frame sent %d more requests", n-1)
	}

	f, err := os.Open(path) //nolint:gosec // A test file.
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = f.Close() }()
	hole, err := unix.Seek(int(f.Fd()), 0, unix.SEEK_HOLE) //nolint:gosec // Descriptors fit an int.
	if err != nil || hole != imagefs.FrameSize {
		t.Fatalf("the first hole is at %d, %v; want frame 1", hole, err)
	}
	data, err := unix.Seek(int(f.Fd()), imagefs.FrameSize, unix.SEEK_DATA) //nolint:gosec // Descriptors fit an int.
	if err != nil || data != 2*imagefs.FrameSize {
		t.Fatalf("data after the hole is at %d, %v; want frame 2", data, err)
	}
}

// Serving to attach fetches the start frames in the background, and the
// disk's reads come back as its next start trace and recent frames.
func TestDiskServePrefetchesAndTraces(t *testing.T) {
	ts := newTestStore(t)
	frames := make([][]byte, 6)
	for i := range frames {
		frames[i] = bytes.Repeat([]byte{byte(i + 1)}, imagefs.FrameSize)
	}
	stored := ts.disk(t, "d2", frames, []uint32{4, 1})
	counting := &countingTransport{}
	d := serveDisks(t, counting)
	d.grantTest(t, ts, stored.id)
	g, err := d.serve(t.Context(), stored.serveRequest(t, true))
	if err != nil {
		t.Fatal(err)
	}
	for !d.cache.cached(g.key(4)) || !d.cache.cached(g.key(1)) {
		select {
		case <-t.Context().Done():
			t.Fatal("the start frames were never prefetched")
		case <-time.After(10 * time.Millisecond):
		}
	}
	if d.cache.cached(g.key(0)) {
		t.Fatal("a frame outside the start trace was prefetched")
	}
	fetched := counting.requests.Load()
	readFile(t, d.path(g), 4*imagefs.FrameSize, 10)
	if counting.requests.Load() != fetched {
		t.Fatal("reading a prefetched frame fetched it again")
	}
	readFile(t, d.path(g), 2*imagefs.FrameSize, 10)
	readFile(t, d.path(g), 1*imagefs.FrameSize, 10)

	reads, err := d.reads(stored.id)
	if err != nil {
		t.Fatal(err)
	}
	if len(reads.GetStartFrames()) != 0 || !slices.Equal(reads.GetRecentFrames(), []uint32{1, 2, 4}) {
		t.Fatalf("within the trace window the disk reports %+v", reads)
	}
	k := d.lookup(stored.id)
	k.mu.Lock()
	k.traceStart = k.traceStart.Add(-diskTraceWindow)
	k.mu.Unlock()
	if reads, err = d.reads(stored.id); err != nil || !slices.Equal(reads.GetStartFrames(), []uint32{4, 2, 1}) {
		t.Fatalf("after the trace window the disk reports %+v, %v", reads, err)
	}

	d.release(stored.id, 0)
	if _, err := os.Stat(filepath.Join(d.dir, g.name)); !os.IsNotExist(err) {
		t.Fatalf("a released generation's file: %v", err)
	}
}

// Only reads of stored frames through the disk's own opens make its start
// trace: not a frame of zeros, nor a read through a file opened with
// O_NOATIME, as a publish opens it.
func TestDiskTraceCountsOnlyTheDisksStoredReads(t *testing.T) {
	ts := newTestStore(t)
	frames := [][]byte{bytes.Repeat([]byte{1}, imagefs.FrameSize), nil, bytes.Repeat([]byte{2}, imagefs.FrameSize)}
	stored := ts.disk(t, "d4", frames, nil)
	d := serveDisks(t, &countingTransport{})
	d.grantTest(t, ts, stored.id)
	g, err := d.serve(t.Context(), stored.serveRequest(t, true))
	if err != nil {
		t.Fatal(err)
	}
	readFile(t, d.path(g), imagefs.FrameSize, 10)
	quiet, err := os.OpenFile(d.path(g), os.O_RDONLY|unix.O_NOATIME, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := quiet.ReadAt(make([]byte, 10), 2*imagefs.FrameSize); err != nil {
		t.Fatal(err)
	}
	_ = quiet.Close()
	readFile(t, d.path(g), 0, 10)
	k := d.lookup(stored.id)
	k.mu.Lock()
	k.traceStart = k.traceStart.Add(-diskTraceWindow)
	k.mu.Unlock()
	reads, err := d.reads(stored.id)
	if err != nil || !slices.Equal(reads.GetStartFrames(), []uint32{0}) {
		t.Fatalf("the disk reports %+v, %v; want a start trace of frame 0", reads, err)
	}
}

// delayedTransport counts requests and holds each for delay first.
type delayedTransport struct {
	countingTransport
	delay time.Duration
}

func (d *delayedTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	time.Sleep(d.delay)
	return d.countingTransport.RoundTrip(r)
}

// Releasing a generation stops its prefetch, and serving it again to attach
// starts no second one.
func TestDiskReleaseStopsItsPrefetch(t *testing.T) {
	ts := newTestStore(t)
	frames, start := make([][]byte, 16), make([]uint32, 16)
	for i := range frames {
		frames[i], start[i] = bytes.Repeat([]byte{byte(i + 1)}, imagefs.FrameSize), uint32(i) //nolint:gosec // A test's frame numbers.
	}
	stored := ts.disk(t, "d3", frames, start)
	slow := &delayedTransport{delay: 100 * time.Millisecond}
	d := serveDisks(t, slow)
	d.grantTest(t, ts, stored.id)
	for range 2 {
		if _, err := d.serve(t.Context(), stored.serveRequest(t, true)); err != nil {
			t.Fatal(err)
		}
	}
	d.release(stored.id, 0)
	d.cache.background.Wait()
	if n := slow.requests.Load(); n > int64(len(frames))/2 {
		t.Fatalf("the released disk sent %d requests for its index and %d start frames", n, len(frames))
	}
}
