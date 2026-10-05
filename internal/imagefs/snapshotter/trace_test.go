package snapshotter

import (
	"archive/tar"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// cachedLayers is a frame cache over layers in the test store, without
// mounts.
type cachedLayers struct {
	frames *frameCache
	layers []*layer
}

func newCachedLayers(t *testing.T, transport http.RoundTripper, fetches int, sizes ...int) cachedLayers {
	t.Helper()
	ts := newTestStore(t)
	g := newGrants(time.Now)
	m, err := newMetrics(prometheus.NewRegistry())
	if err != nil {
		t.Fatal(err)
	}
	frames, err := newFrameCache(t.Context(), t.TempDir(), 256<<20, fetches, g, m, slog.New(slog.DiscardHandler))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(frames.background.Wait)
	out := cachedLayers{frames: frames}
	for _, size := range sizes {
		l := ts.layer(t, buildTar(t, []tarEntry{{hdr: tar.Header{Name: "a", Typeflag: tar.TypeReg, Mode: 0o644}, body: random(size)}}))
		g.put(map[imagefs.Digest]grant{l.index.Layer: {indexURL: l.grant.IndexURL, dataURL: l.grant.DataURL, expires: l.grant.ExpiresAt}})
		url := l.grant.DataURL
		out.layers = append(out.layers, &layer{digest: l.index.Layer, index: l.index, frames: frames,
			data: imagefs.HTTPObject(&http.Client{Transport: transport}, func() string { return url })})
	}
	return out
}

func (c cachedLayers) digests() []imagefs.Digest {
	out := make([]imagefs.Digest, len(c.layers))
	for i, l := range c.layers {
		out[i] = l.digest
	}
	return out
}

// A trace lists the frames of its layers read since it started, each the
// first time, by the layer's position; a trace started while one of its
// layers was mounted is not complete.
func TestTracesRecordFirstReadsInOrder(t *testing.T) {
	c := newCachedLayers(t, http.DefaultTransport, 4, 3*imagefs.FrameSize, 2*imagefs.FrameSize, imagefs.FrameSize)
	base, app, other := c.layers[0], c.layers[1], c.layers[2]
	sources := layerSources{frames: c.frames}
	read := func(l *layer, frame int) {
		t.Helper()
		if err := c.frames.read(l, frame, make([]byte, 8), 0); err != nil {
			t.Fatal(err)
		}
	}
	read(base, 0)
	start := &imagefsproto.StartTraceRequest{Name: "container", Layers: []string{string(base.digest), string(app.digest)}}
	if _, err := sources.StartTrace(t.Context(), start); err != nil {
		t.Fatal(err)
	}
	read(app, 1)
	read(base, 2)
	read(other, 0)
	read(app, 1)
	read(base, 0)
	ended, err := sources.EndTrace(t.Context(), &imagefsproto.EndTraceRequest{Name: "container"})
	if err != nil {
		t.Fatal(err)
	}
	var got [][2]uint32
	for _, r := range ended.GetReads() {
		got = append(got, [2]uint32{r.GetLayer(), r.GetFrame()})
	}
	if want := [][2]uint32{{1, 1}, {0, 2}, {0, 0}}; !slices.Equal(got, want) || !ended.GetComplete() {
		t.Fatalf("the trace is %v, complete %v; want %v, complete", got, ended.GetComplete(), want)
	}
	read(app, 0)
	if _, err := sources.EndTrace(t.Context(), &imagefsproto.EndTraceRequest{Name: "container"}); status.Code(err) != codes.NotFound {
		t.Fatalf("ending an ended trace: %v", err)
	}

	c.frames.setMounted(app, 1)
	if _, err := sources.StartTrace(t.Context(), start); err != nil {
		t.Fatal(err)
	}
	ended, err = sources.EndTrace(t.Context(), &imagefsproto.EndTraceRequest{Name: "container"})
	if err != nil || ended.GetComplete() {
		t.Fatalf("a trace started over a mounted layer ended %v, %v; want incomplete", ended, err)
	}
}

// Traces are bounded in number and size, and one nobody ends expires.
func TestTracesAreBounded(t *testing.T) {
	now := time.Now()
	tr := newTracer(func() time.Time { return now })
	layer := imagefs.Digest("sha256:" + strings.Repeat("0", 64))
	for i := range maxTraces {
		if err := tr.start(fmt.Sprint(i), []imagefs.Digest{layer}, true); err != nil {
			t.Fatal(err)
		}
	}
	if err := tr.start("one more", []imagefs.Digest{layer}, true); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("trace %d started: %v", maxTraces+1, err)
	}
	for frame := range maxTraceReads + 10 {
		tr.record(layer, frame)
	}
	got, ok := tr.end("0")
	if !ok || len(got.reads) != maxTraceReads {
		t.Fatalf("a trace past its bound holds %d reads", len(got.reads))
	}
	now = now.Add(traceLife)
	if _, ok := tr.end("1"); ok {
		t.Fatal("a trace past its life ended with its reads")
	}
	if err := tr.start("one more", []imagefs.Digest{layer}, true); err != nil {
		t.Fatalf("expired traces still count: %v", err)
	}
}

// A prefetch fetches exactly its frames once their layer mounts, and the
// reads that follow wait for no store.
func TestPrefetchFetchesTracedFramesOnceMounted(t *testing.T) {
	transport := &countingTransport{}
	c := newCachedLayers(t, transport, 4, 4*imagefs.FrameSize, 2*imagefs.FrameSize)
	base, app := c.layers[0], c.layers[1]
	sources := layerSources{frames: c.frames}
	request := &imagefsproto.PrefetchRequest{
		Layers: []string{string(base.digest), string(app.digest)},
		Reads:  []*imagefsproto.FrameRead{{Layer: 1, Frame: 1}, {Layer: 0, Frame: 3}, {Layer: 0, Frame: 0}, {Layer: 1, Frame: 1}, {Layer: 0, Frame: 99}},
	}
	if _, err := sources.Prefetch(t.Context(), request); err != nil {
		t.Fatal(err)
	}
	time.Sleep(100 * time.Millisecond)
	if n := transport.requests.Load(); n != 0 {
		t.Fatalf("a prefetch made %d requests before its layers mounted", n)
	}
	c.frames.setMounted(app, 1)
	c.frames.setMounted(base, 1)
	waitFor(t, func() bool { return len(c.frames.prefetching) == 0 })
	if n := transport.requests.Load(); n != 3 {
		t.Fatalf("a prefetch of three frames made %d requests", n)
	}
	for _, r := range [][2]int{{1, 1}, {0, 3}, {0, 0}} {
		if err := c.frames.read(c.layers[r[0]], r[1], make([]byte, 8), 0); err != nil {
			t.Fatal(err)
		}
	}
	if n := transport.requests.Load(); n != 3 {
		t.Fatalf("reading the prefetched frames made %d more requests", n-3)
	}
	bad := &imagefsproto.PrefetchRequest{Layers: request.GetLayers(), Reads: []*imagefsproto.FrameRead{{Layer: 2}}}
	if _, err := sources.Prefetch(t.Context(), bad); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("a prefetch naming a third layer of two: %v", err)
	}
}

// Prefetches hold at most half the fetch slots, so a container's read of
// another frame is fetched while every prefetch fetch waits on the store.
func TestPrefetchesLeaveSlotsForReads(t *testing.T) {
	held := &holdingTransport{release: make(chan struct{})}
	c := newCachedLayers(t, held, 4, 12*imagefs.FrameSize)
	l := c.layers[0]
	c.frames.setMounted(l, 1)
	reads := make([]prefetchRead, 10)
	for i := range reads {
		reads[i] = prefetchRead{layer: l.digest, frame: i}
	}
	if err := c.frames.prefetch(reads); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return held.waiting.Load() == 2 })
	held.pass.Store(true)
	if err := c.frames.read(l, 11, make([]byte, 8), 0); err != nil {
		t.Fatalf("a read while prefetches hold their slots: %v", err)
	}
	if n := held.waiting.Load(); n != 2 {
		t.Fatalf("%d prefetch fetches in flight, want half of 4 slots", n)
	}
	close(held.release)
	waitFor(t, func() bool { return len(c.frames.prefetching) == 0 })
}

// holdingTransport holds requests until release closes, and lets them
// through once pass is set.
type holdingTransport struct {
	release chan struct{}
	pass    atomic.Bool
	waiting atomic.Int64
}

func (h *holdingTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	if !h.pass.Load() {
		h.waiting.Add(1)
		<-h.release
	}
	return http.DefaultTransport.RoundTrip(r)
}

func waitFor(t *testing.T, done func() bool) {
	t.Helper()
	for deadline := time.Now().Add(30 * time.Second); !done(); {
		if time.Now().After(deadline) {
			t.Fatal("timed out")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// Through mounts, a trace of one start lists the frames it read, and a
// later cold start's prefetch of that trace fetches those frames and no
// others, so the same reads then wait for no store.
func TestTracedFramesPrefetchThroughMounts(t *testing.T) {
	ts := newTestStore(t)
	weights := random(5 * imagefs.FrameSize)
	base := ts.layer(t, buildTar(t, []tarEntry{{hdr: tar.Header{Name: "lib", Typeflag: tar.TypeReg, Mode: 0o644}, body: weights[:3*imagefs.FrameSize]}}))
	app := ts.layer(t, buildTar(t, []tarEntry{{hdr: tar.Header{Name: "weights", Typeflag: tar.TypeReg, Mode: 0o644}, body: weights}}))
	layers := []imagefs.Digest{base.index.Layer, app.index.Layer}
	start := func(s *service) (string, string) {
		t.Helper()
		if err := s.sources.Grant(t.Context(), []layersource.Grant{base.grant, app.grant}); err != nil {
			t.Fatal(err)
		}
		return s.view(t, "base", s.pull(t, base, "")), s.view(t, "app", s.pull(t, app, ""))
	}
	readAt := func(path string, off int64) {
		t.Helper()
		f, err := os.Open(path)
		if err != nil {
			t.Fatal(err)
		}
		defer func() { _ = f.Close() }()
		if _, err := f.ReadAt(make([]byte, 4096), off); err != nil {
			t.Fatal(err)
		}
	}
	startup := func(baseDir, appDir string) {
		readAt(filepath.Join(appDir, "weights"), 4*imagefs.FrameSize)
		readAt(filepath.Join(baseDir, "lib"), 0)
		readAt(filepath.Join(appDir, "weights"), imagefs.FrameSize)
	}

	first := serve(t, http.DefaultTransport)
	if err := first.sources.StartTrace(t.Context(), "c1", layers); err != nil {
		t.Fatal(err)
	}
	startup(start(first))
	reads, complete, err := first.sources.EndTrace(t.Context(), "c1")
	if err != nil {
		t.Fatal(err)
	}
	if want := []layersource.FrameRead{{Layer: 1, Frame: 4}, {Layer: 0, Frame: 0}, {Layer: 1, Frame: 1}}; !complete || !slices.Equal(reads, want) {
		t.Fatalf("the first start traced %v, complete %v; want %v", reads, complete, want)
	}
	first.stop()

	transport := &countingTransport{}
	cold := serve(t, transport)
	if err := cold.sources.Prefetch(t.Context(), layers, reads); err != nil {
		t.Fatal(err)
	}
	baseDir, appDir := start(cold)
	const indexes = 2
	waitFor(t, func() bool { return transport.requests.Load() >= indexes+3 })
	time.Sleep(200 * time.Millisecond)
	startup(baseDir, appDir)
	if n := transport.requests.Load(); n != indexes+3 {
		t.Fatalf("a cold start with its trace made %d store requests, want two indexes and three frames", n)
	}
}
