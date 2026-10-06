package snapshotter

import (
	"archive/tar"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	oteltrace "go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// cachedLayers is a frame cache over layers in the test store, without
// mounts.
type cachedLayers struct {
	cache  *frameCache
	layers []*layer
}

func newCachedLayers(t *testing.T, transport http.RoundTripper, sizes ...int) cachedLayers {
	t.Helper()
	ts := newTestStore(t)
	out := cachedLayers{cache: newTestCache(t, transport, 256<<20)}
	for _, size := range sizes {
		l := ts.layer(t, buildTar(t, []tarEntry{{hdr: tar.Header{Name: "a", Typeflag: tar.TypeReg, Mode: 0o644}, body: random(size)}}))
		out.layers = append(out.layers, l.grantTo(out.cache))
	}
	return out
}

// A trace lists the frames of its layers read since it started, each the
// first time, by the layer's position; a trace started while one of its
// layers was mounted is not complete.
func TestTracesRecordFirstReadsInOrder(t *testing.T) {
	c := newCachedLayers(t, http.DefaultTransport, 3*imagefs.FrameSize, 2*imagefs.FrameSize, imagefs.FrameSize)
	base, app, other := c.layers[0], c.layers[1], c.layers[2]
	sources := layerSources{cache: c.cache}
	read := func(l *layer, frame int) {
		t.Helper()
		if err := c.cache.read(l, frame, make([]byte, 8), 0); err != nil {
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

	c.cache.mount(app.index)
	if _, err := sources.StartTrace(t.Context(), start); err != nil {
		t.Fatal(err)
	}
	ended, err = sources.EndTrace(t.Context(), &imagefsproto.EndTraceRequest{Name: "container"})
	if err != nil || ended.GetComplete() {
		t.Fatalf("a trace started over a mounted layer ended %v, %v; want incomplete", ended, err)
	}
}

func digestOf(c byte) imagefs.Digest {
	return imagefs.Digest("sha256:" + strings.Repeat(string(c), 64))
}

// Traces are bounded in number and size, and ones nobody ends expire.
func TestTracesAreBounded(t *testing.T) {
	l := &layer{digest: digestOf('0'), traced: make([]atomic.Uint32, maxTraceReads+10)}
	layers := []imagefs.Digest{l.digest}
	tr := &tracer{traces: map[string]*trace{}}
	for i := range maxTraces {
		if err := tr.start(fmt.Sprint(i), layers, true); err != nil {
			t.Fatal(err)
		}
	}
	if err := tr.start("one more", layers, true); status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("trace %d started: %v", maxTraces+1, err)
	}
	for frame := range l.traced {
		tr.record(l, frame)
	}
	if got, ok := tr.end("0"); !ok || len(got.reads) != maxTraceReads {
		t.Fatalf("a trace past its bound ended %v, %+v", ok, got)
	}
	tr.traces["1"].expires = time.Now()
	if _, ok := tr.end("1"); ok {
		t.Fatal("an expired trace ended with its reads")
	}
	if err := tr.start("one more", layers, true); err != nil {
		t.Fatalf("a trace in an expired one's place: %v", err)
	}
}

// A FUSE read cannot be told apart by container, so another start naming
// one of a trace's layers, by its grant, prefetch or own trace, leaves no
// complete trace; a trace's own start's grant and a refresh naming no start
// keep it complete.
func TestSharedLayersLeaveNoCompleteTrace(t *testing.T) {
	c := newCachedLayers(t, http.DefaultTransport)
	sources := layerSources{cache: c.cache}
	base, appA, appB := string(digestOf('a')), string(digestOf('b')), string(digestOf('c'))
	grant := func(name string, layers ...string) {
		t.Helper()
		request := &imagefsproto.GrantRequest{Name: name}
		for _, l := range layers {
			request.Layers = append(request.Layers, &imagefsproto.LayerGrant{DiffId: l, IndexUrl: "http://store/i", DataUrl: "http://store/d",
				ExpiresAt: timestamppb.New(time.Now().Add(time.Hour))})
		}
		if _, err := sources.Grant(t.Context(), request); err != nil {
			t.Fatal(err)
		}
	}
	trace := func(name string, layers ...string) {
		t.Helper()
		if _, err := sources.StartTrace(t.Context(), &imagefsproto.StartTraceRequest{Name: name, Layers: layers}); err != nil {
			t.Fatal(err)
		}
	}
	complete := func(name string) bool {
		t.Helper()
		ended, err := sources.EndTrace(t.Context(), &imagefsproto.EndTraceRequest{Name: name})
		if err != nil {
			t.Fatal(err)
		}
		return ended.GetComplete()
	}
	others := map[string]func(){
		"another start's grant": func() { grant("c2", base, appB) },
		"another start's prefetch": func() {
			if _, err := sources.Prefetch(t.Context(), &imagefsproto.PrefetchRequest{Name: "c2", Layers: []string{base, appB}}); err != nil {
				t.Fatal(err)
			}
		},
	}
	for what, use := range others {
		trace("c1", base, appA)
		use()
		if complete("c1") {
			t.Fatalf("a trace stayed complete through %s of a shared layer", what)
		}
	}
	trace("c1", base, appA)
	trace("c2", base, appB)
	if complete("c1") || complete("c2") {
		t.Fatal("two traces sharing a layer stayed complete")
	}
	trace("c1", base, appA)
	grant("c1", base, appA)
	grant("c2", appB)
	grant("", base, appA)
	if !complete("c1") {
		t.Fatal("a trace's own grant, a refresh, or another start's grant of other layers left it incomplete")
	}
}

// A prefetch stopped when its start fails frees its place, so failed starts
// never use up the prefetches a host runs; one whose layers never mount
// would otherwise hold its place for its whole life.
func TestFailedStartsNeverExhaustPrefetches(t *testing.T) {
	c := newCachedLayers(t, http.DefaultTransport)
	sources := layerSources{cache: c.cache}
	for i := range 3 * maxPrefetches {
		name := fmt.Sprint("start-", i)
		request := &imagefsproto.PrefetchRequest{Name: name, Layers: []string{string(digestOf('d'))}, Reads: []*imagefsproto.FrameRead{{}}}
		if _, err := sources.Prefetch(t.Context(), request); err != nil {
			t.Fatalf("prefetch of failed start %d: %v", i, err)
		}
		if _, err := sources.StopPrefetch(t.Context(), &imagefsproto.StopPrefetchRequest{Name: name}); err != nil {
			t.Fatal(err)
		}
	}
	ended := make(chan struct{})
	go func() { c.cache.background.Wait(); close(ended) }()
	select {
	case <-ended:
	case <-time.After(time.Second):
		t.Fatal("stopped prefetches kept waiting for their layers to mount")
	}
}

// A prefetch fetches exactly its frames once their layer mounts, and the
// reads that follow wait for no store.
func TestPrefetchFetchesTracedFramesOnceMounted(t *testing.T) {
	transport := &countingTransport{}
	c := newCachedLayers(t, transport, 4*imagefs.FrameSize, 2*imagefs.FrameSize)
	base, app := c.layers[0], c.layers[1]
	sources := layerSources{cache: c.cache}
	request := &imagefsproto.PrefetchRequest{
		Name:   "container",
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
	c.cache.mount(app.index)
	c.cache.mount(base.index)
	c.cache.background.Wait()
	if n := transport.requests.Load(); n != 3 {
		t.Fatalf("a prefetch of three frames made %d requests", n)
	}
	for _, r := range [][2]int{{1, 1}, {0, 3}, {0, 0}} {
		if err := c.cache.read(c.layers[r[0]], r[1], make([]byte, 8), 0); err != nil {
			t.Fatal(err)
		}
	}
	if n := transport.requests.Load(); n != 3 {
		t.Fatalf("reading the prefetched frames made %d more requests", n-3)
	}
	bad := &imagefsproto.PrefetchRequest{Name: "container", Layers: request.GetLayers(), Reads: []*imagefsproto.FrameRead{{Layer: 2}}}
	if _, err := sources.Prefetch(t.Context(), bad); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("a prefetch naming a third layer of two: %v", err)
	}
}

// Prefetches hold at most half the fetch slots, so a container's read of
// another frame is fetched while every prefetch fetch waits on the store.
func TestPrefetchesLeaveSlotsForReads(t *testing.T) {
	held := &holdingTransport{release: make(chan struct{})}
	c := newCachedLayers(t, held, 12*imagefs.FrameSize)
	l := c.layers[0]
	l = c.cache.mount(l.index)
	reads := make([]frameKey, 10)
	for i := range reads {
		reads[i] = frameKey{layer: l.digest, frame: i}
	}
	if err := c.cache.prefetch("container", reads, oteltrace.SpanContext{}); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return held.waiting.Load() == 2 })
	held.pass.Store(true)
	if err := c.cache.read(l, 11, make([]byte, 8), 0); err != nil {
		t.Fatalf("a read while prefetches hold their slots: %v", err)
	}
	if n := held.waiting.Load(); n != 2 {
		t.Fatalf("%d prefetch fetches in flight, want half of 4 slots", n)
	}
	close(held.release)
	c.cache.background.Wait()
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
		if err := s.sources.Grant(t.Context(), "c1", []layersource.Grant{base.grant, app.grant}); err != nil {
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
	if err := cold.sources.Prefetch(t.Context(), "c1", layers, reads); err != nil {
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
