package agent

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/go-containerregistry/pkg/name"
	"github.com/google/go-containerregistry/pkg/v1/remote"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
)

// bytesObject reads ranges of an object held in memory.
type bytesObject []byte

func (b bytesObject) ReadRange(_ context.Context, off, n int64) (io.ReadCloser, error) {
	return io.NopCloser(bytes.NewReader(b[off : off+n])), nil
}

// After its push a build converts each layer the server names and reports
// the sizes, uploads the data in the parts it is given and the index, and
// reports the parts' ETags, until the server names none.
func TestAgentConvertsTheLayersTheServerNames(t *testing.T) {
	e := newEnv(t)
	registry := startTestRegistry(t)
	const partBytes = 1 << 20

	var mu sync.Mutex
	stored := map[string][]byte{}
	failed := map[string]bool{}
	store := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if r.Method != http.MethodPut || err != nil || r.ContentLength != int64(len(body)) {
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		mu.Lock()
		defer mu.Unlock()
		// Every object's first PUT fails as a store under load would.
		if !failed[r.URL.Path] {
			failed[r.URL.Path] = true
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		stored[r.URL.Path] = body
		w.Header().Set("ETag", `"`+r.URL.Path+`"`)
	}))
	t.Cleanup(store.Close)

	type layer struct{ blob, diffID string }
	var layers []layer
	var sizes map[string]int64
	e.server.mu.Lock()
	e.server.answerBuild = func(r *hostproto.CompleteImageBuildRequest) *hostproto.CompleteImageBuildResponse {
		resp := &hostproto.CompleteImageBuildResponse{}
		switch {
		case r.GetDigest() == "" || len(r.GetUploadedLayers()) > 0:
			return resp
		case len(r.GetConvertedLayers()) > 0:
			sizes = map[string]int64{}
			for n, c := range r.GetConvertedLayers() {
				sizes[c.GetBlobDigest()] = c.GetDataBytes()
				prefix := store.URL + "/layers/" + strconv.Itoa(n)
				upload := &hostproto.LayerUpload{
					BlobDigest: c.GetBlobDigest(), DiffId: layers[n].diffID, IndexUrl: prefix + "/index?X-Amz-Signature=secret",
					DataPartBytes: partBytes,
				}
				for p := range (c.GetDataBytes() + partBytes - 1) / partBytes {
					upload.DataPartUrls = append(upload.DataPartUrls, prefix+"/data/"+strconv.Itoa(int(p))+"?X-Amz-Signature=secret")
				}
				resp.LayerUploads = append(resp.LayerUploads, upload)
			}
			return resp
		}
		ref, err := name.NewDigest(registry+"/lazycloud/images@"+r.GetDigest(), name.Insecure)
		if err != nil {
			t.Error(err)
			return nil
		}
		img, err := remote.Image(ref)
		if err != nil {
			t.Error(err)
			return nil
		}
		manifest, err := img.Manifest()
		if err != nil {
			t.Error(err)
			return nil
		}
		config, err := img.ConfigFile()
		if err != nil {
			t.Error(err)
			return nil
		}
		for n, l := range manifest.Layers {
			layers = append(layers, layer{l.Digest.String(), config.RootFS.DiffIDs[n].String()})
			resp.LayerUploads = append(resp.LayerUploads, &hostproto.LayerUpload{BlobDigest: l.Digest.String(), DiffId: config.RootFS.DiffIDs[n].String()})
		}
		return resp
	}
	e.server.mu.Unlock()
	e.startAgent()
	session := e.session()

	start := buildCommand(registry, "FROM "+testBuildBase+"\nRUN <<'LAZYCLOUD_STEP'\nhead -c 3000000 /dev/urandom > /proof\nLAZYCLOUD_STEP\n")
	container := start.GetStart().GetContainerId()
	session.send(t, start)
	var reports []*hostproto.CompleteImageBuildRequest
	for len(reports) < 3 {
		select {
		case r := <-e.server.builds:
			reports = append(reports, r)
		case <-time.After(5 * time.Minute):
			t.Fatalf("the build made %d reports, want 3\noutput:\n%s", len(reports), e.server.buildOutput())
		}
	}
	session.phase(t, container, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
	if reports[1].GetDigest() != reports[0].GetDigest() || len(layers) == 0 || len(reports[1].GetConvertedLayers()) != len(layers) {
		t.Fatalf("the second report sizes every layer: %v", reports[1])
	}
	mu.Lock()
	defer mu.Unlock()
	multipart := false
	for n, l := range layers {
		uploaded := reports[2].GetUploadedLayers()[n]
		if uploaded.GetBlobDigest() != l.blob {
			t.Fatalf("uploaded %s, want %s", uploaded.GetBlobDigest(), l.blob)
		}
		var data []byte
		for p, etag := range uploaded.GetPartEtags() {
			path := "/layers/" + strconv.Itoa(n) + "/data/" + strconv.Itoa(p)
			if etag != `"`+path+`"` {
				t.Fatalf("part %d of layer %d reported ETag %s", p, n, etag)
			}
			data = append(data, stored[path]...)
		}
		multipart = multipart || len(uploaded.GetPartEtags()) > 1
		ix, err := imagefs.Unmarshal(stored["/layers/"+strconv.Itoa(n)+"/index"])
		if err != nil {
			t.Fatal(err)
		}
		if string(ix.Layer) != l.diffID || ix.DataSize != int64(len(data)) || sizes[l.blob] != ix.DataSize {
			t.Fatalf("layer %d: index of %s with %d data bytes, got %d bytes", n, ix.Layer, ix.DataSize, len(data))
		}
		for f := range ix.Frames {
			if _, err := ix.ReadFrame(t.Context(), bytesObject(data), f); err != nil {
				t.Fatal(err)
			}
		}
	}
	if !multipart {
		t.Fatal("no layer was uploaded in more than one part")
	}
	if out := e.server.buildOutput(); !strings.Contains(out, "converted "+strconv.Itoa(len(layers))+" layers") || strings.Contains(out, "secret") {
		t.Fatalf("the output names the conversion and no signature:\n%s", out)
	}
}
