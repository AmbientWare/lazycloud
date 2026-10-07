package agent

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/registry"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/random"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/types"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// pushLayers writes random layers of sizes to a registry for the test and
// returns its push repository and the layers.
func pushLayers(t *testing.T, wrap func(http.Handler) http.Handler, sizes ...int64) (string, []v1.Layer) {
	t.Helper()
	server := httptest.NewServer(wrap(registry.New()))
	t.Cleanup(server.Close)
	repository := strings.TrimPrefix(server.URL, "http://") + "/lazycloud/images"
	repo, err := name.NewRepository(repository, name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	var layers []v1.Layer
	for _, size := range sizes {
		layer, err := random.Layer(size, types.DockerLayer)
		if err != nil {
			t.Fatal(err)
		}
		if err := remote.WriteLayer(repo, layer, remote.WithContext(t.Context())); err != nil {
			t.Fatal(err)
		}
		layers = append(layers, layer)
	}
	return repository, layers
}

// layerServer answers completions as the images owner does: it names every
// layer not yet uploaded, with URLs on store in parts of partBytes once its
// sizes arrived. With again set it names uploaded layers again too.
type layerServer struct {
	store     string
	partBytes int64
	again     bool
	mu        sync.Mutex
	layers    []*testLayer
}

// testLayer is one layer and what the host reported of it: how often it
// sent the layer's sizes and its upload, and the upload's ETags.
type testLayer struct {
	blob, diffID    string
	dataBytes       int64
	sized, uploaded int
	etags           []string
}

// signature is the query of every URL the server signs, which no error or
// output may show.
const signature = "?X-Amz-Signature=secret"

func newLayerServer(t *testing.T, store string, layers []v1.Layer) *layerServer {
	t.Helper()
	s := &layerServer{store: store, partBytes: 1 << 10}
	for _, l := range layers {
		blob, err := l.Digest()
		if err != nil {
			t.Fatal(err)
		}
		diffID, err := l.DiffID()
		if err != nil {
			t.Fatal(err)
		}
		s.layers = append(s.layers, &testLayer{blob: blob.String(), diffID: diffID.String(), dataBytes: -1})
	}
	return s
}

func (s *layerServer) complete(r *hostproto.CompleteImageBuildRequest) *hostproto.CompleteImageBuildResponse {
	s.mu.Lock()
	defer s.mu.Unlock()
	resp := &hostproto.CompleteImageBuildResponse{}
	for n, l := range s.layers {
		for _, c := range r.GetConvertedLayers() {
			if c.GetBlobDigest() == l.blob {
				l.dataBytes = c.GetDataBytes()
				l.sized++
			}
		}
		for _, u := range r.GetUploadedLayers() {
			if u.GetBlobDigest() == l.blob {
				l.etags = u.GetPartEtags()
				l.uploaded++
			}
		}
		if l.uploaded > 0 && !s.again {
			continue
		}
		upload := &hostproto.LayerUpload{BlobDigest: l.blob, DiffId: l.diffID}
		if l.dataBytes >= 0 {
			prefix := s.store + "/" + strconv.Itoa(n)
			upload.IndexUrl, upload.DataPartBytes = prefix+"/index"+signature, s.partBytes
			for p := range (l.dataBytes + s.partBytes - 1) / s.partBytes {
				upload.DataPartUrls = append(upload.DataPartUrls, prefix+"/data/"+strconv.Itoa(int(p))+signature)
			}
		}
		resp.LayerUploads = append(resp.LayerUploads, upload)
	}
	return resp
}

// testPublish is a publish of a build pushed to repository, completed on a
// test server that answers as layers does. Its output is never sent.
func testPublish(t *testing.T, repository string, layers *layerServer) *layerPublish {
	t.Helper()
	server := newHostServer()
	server.answerBuild = layers.complete
	ctx, stop := context.WithCancel(context.Background())
	go func() {
		for {
			select {
			case <-server.builds:
			case <-ctx.Done():
				return
			}
		}
	}()
	t.Cleanup(stop)
	build := &hostproto.ImageBuild{PushRepository: repository, InsecureRegistry: true}
	return newLayerPublish(newTestContainer(t, server), build, t.TempDir(), "", newBuildLogs(nil, "build", nil))
}

// A converted layer uploads while another still converts: here the large
// layer's blob is only served once the small layer's index is stored.
func TestLayerPublishUploadsALayerWhileAnotherConverts(t *testing.T) {
	smallStored := make(chan struct{})
	var once sync.Once
	var mu sync.Mutex
	puts := map[string]int{}
	store := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		mu.Lock()
		puts[r.URL.Path]++
		mu.Unlock()
		if r.URL.Path == "/0/index" {
			once.Do(func() { close(smallStored) })
		}
		w.Header().Set("ETag", `"`+r.URL.Path+`"`)
	}))
	t.Cleanup(store.Close)
	var large atomic.Value
	repository, layers := pushLayers(t, func(h http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if l, _ := large.Load().(string); l != "" && r.Method == http.MethodGet && strings.HasSuffix(r.URL.Path, "/blobs/"+l) {
				select {
				case <-smallStored:
				case <-time.After(20 * time.Second):
					t.Error("the large layer's blob was read before the small layer was stored")
				}
			}
			h.ServeHTTP(w, r)
		})
	}, 2<<10, 64<<10)
	digest, err := layers[1].Digest()
	if err != nil {
		t.Fatal(err)
	}
	large.Store(digest.String())
	server := newLayerServer(t, store.URL, layers)
	start := time.Now()
	if err := testPublish(t, repository, server).publish(t.Context(), pushedBuild("build", "sha256:"+strings.Repeat("0", 64))); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) > 15*time.Second {
		t.Fatal("the small layer was stored only after the large one converted")
	}
	for n, l := range server.layers {
		if l.uploaded != 1 || puts["/"+strconv.Itoa(n)+"/index"] != 1 {
			t.Fatalf("layer %d was not uploaded once: %v", n, puts)
		}
	}
}

// A layer the server keeps naming after its upload fails the build for
// itself once it has taken maxPublishRounds.
func TestLayerPublishBoundsTheRoundsOfALayer(t *testing.T) {
	var mu sync.Mutex
	indexPuts := 0
	store := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = io.Copy(io.Discard, r.Body)
		mu.Lock()
		defer mu.Unlock()
		if strings.HasSuffix(r.URL.Path, "/index") {
			indexPuts++
		}
		w.Header().Set("ETag", `"x"`)
	}))
	t.Cleanup(store.Close)
	repository, layers := pushLayers(t, func(h http.Handler) http.Handler { return h }, 2<<10)
	server := newLayerServer(t, store.URL, layers)
	server.again = true
	err := testPublish(t, repository, server).publish(t.Context(), pushedBuild("build", "sha256:"+strings.Repeat("0", 64)))
	if err == nil || errors.Is(err, errLayerContent) || !strings.Contains(err.Error(), "rounds") {
		t.Fatalf("a layer named without end gave %v, want a bounded failure of the build", err)
	}
	if indexPuts != maxPublishRounds-1 {
		t.Fatalf("the layer was uploaded %d times, want %d", indexPuts, maxPublishRounds-1)
	}
}

// cutWriter passes on the first n bytes of a response and drops the rest.
type cutWriter struct {
	http.ResponseWriter
	n int
}

func (w *cutWriter) Write(p []byte) (int, error) {
	if len(p) > w.n {
		_, _ = w.ResponseWriter.Write(p[:w.n])
		w.n = 0
		return 0, errors.New("cut off")
	}
	w.n -= len(p)
	return w.ResponseWriter.Write(p)
}

// A registry that cuts a layer's stream off once only delays its
// conversion: the cut reads as a truncated tar, which is the read's
// failure, not the layer's.
func TestLayerConversionRetriesACutOffRead(t *testing.T) {
	var cut atomic.Bool
	repository, layers := pushLayers(t, func(h http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.Method == http.MethodGet && strings.Contains(r.URL.Path, "/blobs/") && cut.CompareAndSwap(false, true) {
				w = &cutWriter{ResponseWriter: w, n: 4 << 10}
			}
			h.ServeHTTP(w, r)
		})
	}, 64<<10)
	server := newLayerServer(t, "http://store", layers)
	upload := server.complete(&hostproto.CompleteImageBuildRequest{}).GetLayerUploads()[0]
	if _, err := testPublish(t, repository, server).convert(t.Context(), upload.GetBlobDigest(), upload.GetDiffId()); err != nil || !cut.Load() {
		t.Fatalf("a layer whose first read was cut off gave %v (cut %v)", err, cut.Load())
	}
}

// A host that cannot write its data file fails the build for itself; only
// a layer's own content fails the image.
func TestHostErrorsAreNotLayerContent(t *testing.T) {
	repository, layers := pushLayers(t, func(h http.Handler) http.Handler { return h }, 1024)
	digest, err := layers[0].Digest()
	if err != nil {
		t.Fatal(err)
	}
	ref, err := name.NewDigest(repository+"@"+digest.String(), name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	diffID, err := layers[0].DiffID()
	if err != nil {
		t.Fatal(err)
	}

	missing := &layerPublish{dir: filepath.Join(t.TempDir(), "gone"), auth: authn.Anonymous}
	if _, err := missing.convertLayer(t.Context(), ref, diffID.String()); err == nil || errors.Is(err, errLayerContent) {
		t.Fatalf("an unwritable data directory gave %v, want a host error", err)
	}
	mismatched := &layerPublish{dir: t.TempDir(), auth: authn.Anonymous}
	if _, err := mismatched.convertLayer(t.Context(), ref, "sha256:"+strings.Repeat("0", 64)); !errors.Is(err, errLayerContent) {
		t.Fatalf("a layer that is not its config's diff_id gave %v, want its content refused", err)
	}
}
