package snapshotter

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"io/fs"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"go.opentelemetry.io/otel/trace/noop"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// testStore holds layers in a test bucket, read through
// presigned URLs as hosts read the platform store.
type testStore struct {
	client  *s3.Client
	bucket  string
	prefix  string
	presign *s3.PresignClient
}

func newTestStore(t *testing.T) *testStore {
	t.Helper()
	client := storagetest.Client()
	return &testStore{client: client, bucket: storagetest.Config(t).Bucket, prefix: "snapshotter-test/" + uuid.NewString() + "/", presign: s3.NewPresignClient(client)}
}

func (ts *testStore) put(t *testing.T, key string, body []byte) {
	t.Helper()
	if _, err := ts.client.PutObject(t.Context(), &s3.PutObjectInput{
		Bucket: aws.String(ts.bucket), Key: aws.String(ts.prefix + key), Body: bytes.NewReader(body),
	}); err != nil {
		t.Fatal(err)
	}
}

func (ts *testStore) remove(t *testing.T, key string) {
	t.Helper()
	if _, err := ts.client.DeleteObject(t.Context(), &s3.DeleteObjectInput{Bucket: aws.String(ts.bucket), Key: aws.String(ts.prefix + key)}); err != nil {
		t.Fatal(err)
	}
}

func (ts *testStore) url(t *testing.T, key string) string {
	t.Helper()
	req, err := ts.presign.PresignGetObject(t.Context(), &s3.GetObjectInput{Bucket: aws.String(ts.bucket), Key: aws.String(ts.prefix + key)},
		s3.WithPresignExpires(time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	return req.URL
}

// storedLayer is a converted layer in the test store.
type storedLayer struct {
	index imagefs.Index
	grant *imagefsproto.LayerGrant
}

func (ts *testStore) layer(t *testing.T, tarball []byte) storedLayer {
	t.Helper()
	var data bytes.Buffer
	ix, err := imagefs.Convert(t.Context(), bytes.NewReader(tarball), &data)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := ix.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	name := strings.TrimPrefix(string(ix.Layer), "sha256:")
	ts.put(t, name+"/index", raw)
	ts.put(t, name+"/data", data.Bytes())
	return storedLayer{index: ix, grant: &imagefsproto.LayerGrant{
		DiffId: string(ix.Layer), IndexUrl: ts.url(t, name+"/index"), DataUrl: ts.url(t, name+"/data"), ExpiresAt: timestamppb.New(time.Now().Add(time.Hour)),
	}}
}

// file is a stored layer of one file, name, holding body.
func (ts *testStore) file(t *testing.T, name string, body []byte) storedLayer {
	t.Helper()
	return ts.layer(t, buildTar(t, []tarEntry{{hdr: tar.Header{Name: name, Typeflag: tar.TypeReg, Mode: 0o644}, body: body}}))
}

// countingTransport counts requests and fails those fail picks.
type countingTransport struct {
	requests atomic.Int64
	fail     func(*http.Request) bool
}

func (c *countingTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	c.requests.Add(1)
	if c.fail != nil && c.fail(r) {
		return &http.Response{StatusCode: http.StatusInternalServerError, Body: http.NoBody, Request: r, Header: http.Header{}}, nil
	}
	return http.DefaultTransport.RoundTrip(r)
}

// testConfig is a snapshotter's configuration for a test under dir.
func testConfig(dir string, transport http.RoundTripper, logger *slog.Logger) Config {
	return Config{
		Root: dir, CacheDir: filepath.Join(dir, "cache"), CacheBytes: 256 << 20, Fetches: 4, VolumeAddr: "127.0.0.1:0", VolumeDisk: true,
		HTTP: &http.Client{Transport: transport}, Logger: logger, Tracer: noop.NewTracerProvider().Tracer(""),
	}
}

type tarEntry struct {
	hdr  tar.Header
	body []byte
}

// buildTar writes a layer whose root the test's user owns, since overlay
// snapshots on top take their owner from it.
func buildTar(t *testing.T, entries []tarEntry) []byte {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	root := tarEntry{hdr: tar.Header{Name: "./", Typeflag: tar.TypeDir, Mode: 0o755, Uid: os.Getuid(), Gid: os.Getgid()}}
	for _, e := range append([]tarEntry{root}, entries...) {
		hdr := e.hdr
		hdr.Size = int64(len(e.body))
		if hdr.ModTime.IsZero() {
			hdr.ModTime = time.Unix(1700000000, 0)
		}
		hdr.Format = tar.FormatPAX
		if err := tw.WriteHeader(&hdr); err != nil {
			t.Fatal(err)
		}
		if _, err := tw.Write(e.body); err != nil {
			t.Fatal(err)
		}
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func random(n int) []byte {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return b
}

// newTestCache is a frame cache in a test directory, reading through
// transport.
func newTestCache(t *testing.T, transport http.RoundTripper, bound int64) *frameCache {
	t.Helper()
	cfg := testConfig("", transport, slog.New(slog.DiscardHandler))
	cfg.CacheBytes = bound
	c, err := newFrameCache(t.Context(), t.TempDir(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	// The writer stores its last frame before the directory goes.
	t.Cleanup(c.writer.Wait)
	t.Cleanup(c.background.Wait)
	return c
}

// Mounts stay up after Close, so reads whose frames arrive once the cache
// stopped still decode them.
func TestReadsOutliveClose(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	held := &holdingTransport{release: make(chan struct{})}
	life, stop := context.WithCancel(t.Context())
	c, err := newFrameCache(life, t.TempDir(), testConfig("", held, slog.New(slog.DiscardHandler)))
	if err != nil {
		t.Fatal(err)
	}
	c.limit = 64 << 20
	l := ts.file(t, "a", random(4*imagefs.FrameSize)).grantTo(c)
	reads := make(chan error, len(l.index.Frames))
	for frame := range l.index.Frames {
		go func() { reads <- l.read(frame, make([]byte, 8), 0) }()
	}
	waitFor(t, func() bool { return held.waiting.Load() == int64(len(l.index.Frames)) })
	stop()
	c.background.Wait()
	close(held.release)
	for range l.index.Frames {
		if err := <-reads; err != nil {
			t.Fatalf("a read whose frame arrived after close: %v", err)
		}
	}
}

// grantTo grants the stored layer to c and returns an unmounted layer of it.
func (l storedLayer) grantTo(c *frameCache) *layer {
	c.grants.put(l.index.Layer, l.grant.GetExpiresAt().AsTime(), func() layerURLs {
		return layerURLs{index: l.grant.GetIndexUrl(), data: l.grant.GetDataUrl()}
	})
	return c.newLayer(l.index)
}

// Cancelling a fill never fails a read waiting on the frame it fetches.
func TestCancelledFillsFinishSharedFetches(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	held := &holdingTransport{release: make(chan struct{}), abort: true}
	c := newTestCache(t, held, 64<<20)
	c.fillBytes = 64 << 20
	l := ts.file(t, "a", random(1<<20)).grantTo(c)
	fillCtx, stopFill := context.WithCancel(t.Context())
	filled := make(chan struct{})
	go func() { c.fillLayer(fillCtx, l); close(filled) }()
	waitFor(t, func() bool { return held.waiting.Load() > 0 })
	// The read joins the fill's fetch or, once that stored the frame, reads
	// it from the cache: either way the frame is fetched once.
	read := make(chan error, 1)
	go func() { read <- l.read(0, make([]byte, 16), 0) }()
	stopFill()
	close(held.release)
	if err := <-read; err != nil {
		t.Fatalf("a read sharing a cancelled fill's fetch: %v", err)
	}
	<-filled
	if n := held.waiting.Load(); n != 1 {
		t.Fatalf("the frame was fetched %d times: the cancelled fill's fetch did not finish", n)
	}
}

// An eviction that cannot delete a frame's file keeps the frame counted, so
// the cache never holds more on disk than it counts, and the next eviction
// tries the frame again.
func TestFailedEvictionsKeepTheirFramesCounted(t *testing.T) {
	t.Parallel()
	c := newTestCache(t, http.DefaultTransport, 8)
	key := func(frame int) frameKey { return frameKey{object: string(digestOf('e')), frame: frame} }
	store := func(frame int, step string) {
		t.Helper()
		if err := c.store(key(frame), []byte("12345")); err != nil {
			t.Fatal(err)
		}
		c.mu.Lock()
		used := c.used
		c.mu.Unlock()
		if onDisk := diskBytes(t, c.dir); used != onDisk {
			t.Fatalf("%s the cache counts %d bytes and holds %d on disk", step, used, onDisk)
		}
	}
	store(0, "after a store")
	// A frame's file that became a directory is not removed as a file.
	stuck := c.touch(key(0)).path
	if err := os.Remove(stuck); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(stuck, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stuck, "held"), []byte("12345"), 0o600); err != nil {
		t.Fatal(err)
	}
	store(1, "after a failed eviction")
	if err := os.RemoveAll(stuck); err != nil {
		t.Fatal(err)
	}
	store(2, "after the next eviction")
	if c.touch(key(0)) != nil || c.touch(key(2)) == nil {
		t.Fatal("the next eviction kept the frame it failed to remove, or dropped the newest")
	}
}

// Concurrent reads of one frame fetch it from the store once.
func TestConcurrentReadsFetchAFrameOnce(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	transport := &countingTransport{}
	c := newTestCache(t, transport, 64<<20)
	l := ts.file(t, "a", random(6<<20)).grantTo(c)
	var wg sync.WaitGroup
	errs := make(chan error, 32)
	for range 32 {
		wg.Go(func() {
			p := make([]byte, 4096)
			errs <- l.read(1, p, 100)
		})
	}
	wg.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if n := transport.requests.Load(); n != 1 {
		t.Fatalf("32 concurrent reads of one frame made %d requests", n)
	}
}

// The cache keeps under its bound while two layers alternate, evicting the
// unmounted layer's frames before the mounted one's.
func TestFrameCacheStaysUnderItsBound(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	const bound = 4 * imagefs.FrameSize
	c := newTestCache(t, http.DefaultTransport, bound)
	layerOf := func(size int) *layer {
		return ts.file(t, "a", random(size)).grantTo(c)
	}
	mounted := c.mount(layerOf(2 * imagefs.FrameSize).index)
	other := layerOf(6 * imagefs.FrameSize)
	readAll := func(l *layer) {
		for i := range l.index.Frames {
			if err := l.read(i, make([]byte, 1), 0); err != nil {
				t.Fatal(err)
			}
			if onDisk := diskBytes(t, c.dir); onDisk > bound {
				t.Fatalf("the cache holds %d bytes on disk, bound %d", onDisk, bound)
			}
		}
	}
	for range 2 {
		readAll(mounted)
		readAll(other)
	}
	for i := range mounted.index.Frames {
		if c.touch(mounted.key(i)) == nil {
			t.Fatalf("frame %d of the mounted layer was evicted before the unmounted layer's", i)
		}
	}
	if c.used > bound {
		t.Fatalf("the cache counts %d bytes, bound %d", c.used, bound)
	}
}

func diskBytes(t *testing.T, dir string) int64 {
	t.Helper()
	var total int64
	err := filepath.WalkDir(dir, func(_ string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		total += info.Size()
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return total
}
