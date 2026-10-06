package agent

import (
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
// layer not yet uploaded, with URLs on store once its sizes arrived. With
// again set it names uploaded layers again too.
type layerServer struct {
	store  string
	again  bool
	mu     sync.Mutex
	layers []*testLayer
}

type testLayer struct {
	blob, diffID string
	dataBytes    int64
	uploaded     bool
}

func newLayerServer(t *testing.T, store string, layers []v1.Layer) *layerServer {
	t.Helper()
	s := &layerServer{store: store}
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
			}
		}
		for _, u := range r.GetUploadedLayers() {
			l.uploaded = l.uploaded || u.GetBlobDigest() == l.blob
		}
		if l.uploaded && !s.again {
			continue
		}
		upload := &hostproto.LayerUpload{BlobDigest: l.blob, DiffId: l.diffID}
		if l.dataBytes >= 0 {
			const partBytes = 1 << 10
			prefix := s.store + "/" + strconv.Itoa(n)
			upload.IndexUrl, upload.DataPartBytes = prefix+"/index", partBytes
			for p := range (l.dataBytes + partBytes - 1) / partBytes {
				upload.DataPartUrls = append(upload.DataPartUrls, prefix+"/data/"+strconv.Itoa(int(p)))
			}
		}
		resp.LayerUploads = append(resp.LayerUploads, upload)
	}
	return resp
}

func testPublish(t *testing.T, repository string) *layerPublish {
	t.Helper()
	build := &hostproto.ImageBuild{PushRepository: repository, InsecureRegistry: true}
	return newLayerPublish(&Agent{http: http.DefaultClient}, build, t.TempDir(), newBuildLogs(nil, "build", nil))
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
	if err := testPublish(t, repository).publish(t.Context(), pushedBuild("build", "sha256:"+strings.Repeat("0", 64)), server.complete); err != nil {
		t.Fatal(err)
	}
	if time.Since(start) > 15*time.Second {
		t.Fatal("the small layer was stored only after the large one converted")
	}
	for n, l := range server.layers {
		if !l.uploaded || puts["/"+strconv.Itoa(n)+"/index"] != 1 {
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
	err := testPublish(t, repository).publish(t.Context(), pushedBuild("build", "sha256:"+strings.Repeat("0", 64)), server.complete)
	if err == nil || errors.Is(err, errLayerContent) || !strings.Contains(err.Error(), "rounds") {
		t.Fatalf("a layer named without end gave %v, want a bounded failure of the build", err)
	}
	if indexPuts != maxPublishRounds-1 {
		t.Fatalf("the layer was uploaded %d times, want %d", indexPuts, maxPublishRounds-1)
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

	missing := &layerPublish{dir: filepath.Join(t.TempDir(), "gone")}
	if _, _, err := missing.convertLayer(t.Context(), ref, authn.Anonymous, diffID.String()); err == nil || errors.Is(err, errLayerContent) {
		t.Fatalf("an unwritable data directory gave %v, want a host error", err)
	}
	mismatched := &layerPublish{dir: t.TempDir()}
	if _, _, err := mismatched.convertLayer(t.Context(), ref, authn.Anonymous, "sha256:"+strings.Repeat("0", 64)); !errors.Is(err, errLayerContent) {
		t.Fatalf("a layer that is not its config's diff_id gave %v, want its content refused", err)
	}
}
