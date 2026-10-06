package agent

import (
	"bytes"
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

// After its push a build converts each layer the server names and reports
// its sizes, uploads the data in the parts it is given and the index, and
// reports the parts' ETags, until the server names none. The server here
// signs a layer's URLs once its sizes arrive, as the images owner does.
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

	type layer struct {
		blob, diffID string
		dataBytes    int64
		// sized and uploaded count the reports of the layer's sizes and
		// of its upload.
		sized, uploaded int
		etags           []string
	}
	var layers []*layer
	e.server.mu.Lock()
	e.server.answerBuild = func(r *hostproto.CompleteImageBuildRequest) *hostproto.CompleteImageBuildResponse {
		mu.Lock()
		defer mu.Unlock()
		resp := &hostproto.CompleteImageBuildResponse{}
		if r.GetDigest() == "" {
			return resp
		}
		if layers == nil {
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
				layers = append(layers, &layer{blob: l.Digest.String(), diffID: config.RootFS.DiffIDs[n].String(), dataBytes: -1})
			}
		}
		for _, l := range layers {
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
		}
		for n, l := range layers {
			if l.uploaded > 0 {
				continue
			}
			upload := &hostproto.LayerUpload{BlobDigest: l.blob, DiffId: l.diffID}
			if l.dataBytes >= 0 {
				prefix := store.URL + "/layers/" + strconv.Itoa(n)
				upload.IndexUrl, upload.DataPartBytes = prefix+"/index?X-Amz-Signature=secret", partBytes
				for p := range (l.dataBytes + partBytes - 1) / partBytes {
					upload.DataPartUrls = append(upload.DataPartUrls, prefix+"/data/"+strconv.Itoa(int(p))+"?X-Amz-Signature=secret")
				}
			}
			resp.LayerUploads = append(resp.LayerUploads, upload)
		}
		return resp
	}
	e.server.mu.Unlock()
	e.startAgent()
	session := e.session()

	start := buildCommand(registry, "FROM "+testBuildBase+"\nRUN <<'LAZYCLOUD_STEP'\nhead -c 3000000 /dev/urandom > /proof\nLAZYCLOUD_STEP\n")
	container := start.GetStart().GetContainerId()
	// The build reports once per conversion or upload that ends between
	// two answers; the reports are drained until it exits.
	exited, reports := make(chan struct{}), make(chan int, 1)
	go func() {
		n := 0
		for {
			select {
			case <-e.server.builds:
				n++
			case <-exited:
				reports <- n
				return
			}
		}
	}()
	session.send(t, start)
	session.phase(t, container, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
	close(exited)
	if n := <-reports; n < 3 {
		t.Fatalf("the build made %d reports, want at least 3\noutput:\n%s", n, e.server.buildOutput())
	}
	mu.Lock()
	defer mu.Unlock()
	if len(layers) == 0 {
		t.Fatal("the server never read the pushed image")
	}
	multipart := false
	for n, l := range layers {
		if l.sized != 1 || l.uploaded != 1 {
			t.Fatalf("layer %d was sized %d and uploaded %d times, want once each", n, l.sized, l.uploaded)
		}
		var data []byte
		for p, etag := range l.etags {
			path := "/layers/" + strconv.Itoa(n) + "/data/" + strconv.Itoa(p)
			if etag != `"`+path+`"` {
				t.Fatalf("part %d of layer %d reported ETag %s", p, n, etag)
			}
			data = append(data, stored[path]...)
		}
		multipart = multipart || len(l.etags) > 1
		ix, err := imagefs.Unmarshal(stored["/layers/"+strconv.Itoa(n)+"/index"])
		if err != nil {
			t.Fatal(err)
		}
		if string(ix.Layer) != l.diffID || ix.DataSize != int64(len(data)) || l.dataBytes != ix.DataSize {
			t.Fatalf("layer %d: index of %s with %d data bytes, got %d bytes", n, ix.Layer, ix.DataSize, len(data))
		}
		object := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.ServeContent(w, r, "", time.Time{}, bytes.NewReader(data))
		}))
		t.Cleanup(object.Close)
		for f := range ix.Frames {
			if _, err := ix.ReadFrame(t.Context(), imagefs.HTTPObject(object.Client(), func() string { return object.URL }), f); err != nil {
				t.Fatal(err)
			}
		}
	}
	if !multipart {
		t.Fatal("no layer was uploaded in more than one part")
	}
	if out := e.server.buildOutput(); !strings.Contains(out, "converted and stored "+strconv.Itoa(len(layers))+" layers") || strings.Contains(out, "secret") {
		t.Fatalf("the output names the conversion and no signature:\n%s", out)
	}
}
