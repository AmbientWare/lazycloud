package images_test

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/mutate"
	"github.com/google/go-containerregistry/pkg/v1/random"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/types"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// layerTar is an uncompressed layer holding one file.
func layerTar(t *testing.T, name string, contents []byte) []byte {
	t.Helper()
	var buf bytes.Buffer
	tw := tar.NewWriter(&buf)
	if err := tw.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(contents)), Typeflag: tar.TypeReg}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(contents); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

// putURL uploads body to a presigned PUT URL and returns the ETag.
func putURL(ctx context.Context, t *testing.T, target string, body []byte) string {
	t.Helper()
	req, err := http.NewRequestWithContext(ctx, http.MethodPut, target, bytes.NewReader(body))
	if err != nil {
		t.Error(err)
		return ""
	}
	if len(body) == 0 {
		req.Body = http.NoBody
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Error(err)
		return ""
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Errorf("upload: %s", resp.Status)
	}
	return resp.Header.Get("ETag")
}

// putPair uploads a pair to the URLs of upload, data in parts of partBytes,
// and returns the parts' ETags.
func putPair(ctx context.Context, t *testing.T, index string, parts []string, partBytes int64, data, encoded []byte) []string {
	t.Helper()
	etags := make([]string, len(parts))
	for n, part := range parts {
		offset := int64(n) * partBytes
		etags[n] = putURL(ctx, t, part, data[offset:min(offset+partBytes, int64(len(data)))])
	}
	putURL(ctx, t, index, encoded)
	return etags
}

// storeLayer converts layer and stores its pair under id through the
// presigned uploads a host gets, and returns its index.
func storeLayer(t *testing.T, store *storage.Storage, id uuid.UUID, layer []byte) imagefs.Index {
	t.Helper()
	var data bytes.Buffer
	ix, err := imagefs.Convert(t.Context(), bytes.NewReader(layer), &data)
	if err != nil {
		t.Fatal(err)
	}
	encoded, err := ix.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	storePair(t.Context(), t, store, id, data.Bytes(), encoded)
	return ix
}

// storePair stores data and index as pair id.
func storePair(ctx context.Context, t *testing.T, store *storage.Storage, id uuid.UUID, data, index []byte) {
	t.Helper()
	uploadID, err := store.CreateLayerUpload(ctx, id, int64(len(data)))
	if err != nil {
		t.Fatal(err)
	}
	urls, err := store.PresignLayerUpload(ctx, id, uploadID, int64(len(data)), int64(len(index)), time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	etags := putPair(ctx, t, urls.Index, urls.DataParts, storage.LayerPartBytes, data, index)
	if err := store.CompleteLayerUpload(ctx, id, uploadID, etags); err != nil {
		t.Fatal(err)
	}
}

// fakeHost does a host's part of publishing an image in repository: it
// converts the layers the server names, reports their sizes and uploads
// them once it has URLs.
type fakeHost struct {
	repository string
	// convert replaces a layer's conversion when set.
	convert func(u images.LayerUpload) (data, index []byte)
	done    map[string][2][]byte
}

func newFakeHost(repository string) *fakeHost {
	return &fakeHost{repository: repository, done: map[string][2][]byte{}}
}

// answer returns the next report's converted and uploaded layers.
func (h *fakeHost) answer(t *testing.T, uploads []images.LayerUpload) ([]images.ConvertedLayer, []images.UploadedLayer) {
	t.Helper()
	var converted []images.ConvertedLayer
	var uploaded []images.UploadedLayer
	for _, u := range uploads {
		pair, ok := h.done[u.Blob]
		if !ok {
			pair = h.convertLayer(t, u)
			h.done[u.Blob] = pair
		}
		if u.Index == "" {
			converted = append(converted, images.ConvertedLayer{Blob: u.Blob, DataBytes: int64(len(pair[0])), IndexBytes: int64(len(pair[1]))})
			continue
		}
		etags := putPair(context.WithoutCancel(t.Context()), t, u.Index, u.DataParts, u.PartBytes, pair[0], pair[1])
		uploaded = append(uploaded, images.UploadedLayer{Blob: u.Blob, ETags: etags})
	}
	return converted, uploaded
}

func (h *fakeHost) convertLayer(t *testing.T, u images.LayerUpload) [2][]byte {
	t.Helper()
	if h.convert != nil {
		data, index := h.convert(u)
		return [2][]byte{data, index}
	}
	ref, err := name.NewDigest(h.repository+"@"+u.Blob, name.Insecure)
	if err != nil {
		t.Error(err)
		return [2][]byte{}
	}
	layer, err := remote.Layer(ref, remote.WithContext(context.WithoutCancel(t.Context())))
	if err != nil {
		t.Error(err)
		return [2][]byte{}
	}
	tarball, err := layer.Uncompressed()
	if err != nil {
		t.Error(err)
		return [2][]byte{}
	}
	defer func() { _ = tarball.Close() }()
	var data bytes.Buffer
	ix, err := imagefs.Convert(t.Context(), tarball, &data)
	if err != nil || string(ix.Layer) != u.DiffID {
		t.Errorf("convert %s: %s %v", u.Blob, ix.Layer, err)
		return [2][]byte{}
	}
	encoded, err := ix.Marshal()
	if err != nil {
		t.Error(err)
	}
	return [2][]byte{data.Bytes(), encoded}
}

// report sends one completion with what the host answered to uploads.
func (h *fakeHost) report(t *testing.T, f fixture, host compute.HostID, container execution.ContainerID, digest string, uploads []images.LayerUpload) ([]images.LayerUpload, error) {
	t.Helper()
	outcome := images.BuildOutcome{Digest: digest}
	outcome.Converted, outcome.Uploaded = h.answer(t, uploads)
	return f.images.CompleteBuild(context.WithoutCancel(t.Context()), host, container, outcome)
}

// publish completes a build that pushed digest to repository, converting
// the layers the server asks for as a host would, and returns how many it
// converted.
func (f fixture) publish(t *testing.T, host compute.HostID, container execution.ContainerID, repository, digest string) int {
	t.Helper()
	h := newFakeHost(repository)
	var uploads []images.LayerUpload
	for range 4 {
		var err error
		uploads, err = h.report(t, f, host, container, digest, uploads)
		if err != nil {
			t.Error(err)
			return len(h.done)
		}
		if len(uploads) == 0 {
			return len(h.done)
		}
	}
	t.Error("the build did not publish")
	return len(h.done)
}

// pushImage writes img to ref and returns its digest.
func pushImage(t *testing.T, ref string, img v1.Image) string {
	t.Helper()
	parsed, err := name.ParseReference(ref, name.Insecure)
	if err != nil {
		t.Fatal(err)
	}
	if err := remote.Write(parsed, img, remote.WithContext(t.Context())); err != nil {
		t.Fatalf("push %s: %v", ref, err)
	}
	digest, err := img.Digest()
	if err != nil {
		t.Fatal(err)
	}
	return digest.String()
}

// onBase is base with one random layer on top.
func onBase(t *testing.T, base v1.Image) v1.Image {
	t.Helper()
	layer, err := random.Layer(4096, types.DockerLayer)
	if err != nil {
		t.Fatal(err)
	}
	img, err := mutate.AppendLayers(base, layer)
	if err != nil {
		t.Fatal(err)
	}
	return img
}

func diffIDs(t *testing.T, img v1.Image) []imagefs.Digest {
	t.Helper()
	config, err := img.ConfigFile()
	if err != nil {
		t.Fatal(err)
	}
	out := make([]imagefs.Digest, len(config.RootFS.DiffIDs))
	for n, d := range config.RootFS.DiffIDs {
		out[n] = imagefs.Digest(d.String())
	}
	return out
}

// startBuild requests a build of def for ws and places its container on
// host.
func (f fixture) startBuild(t *testing.T, ws identity.WorkspaceID, def apitypes.ImageDefinition, host compute.HostID) (images.Resolution, execution.ContainerID) {
	t.Helper()
	r, err := f.images.Build(t.Context(), ws, def, false)
	if err != nil || r.Build == nil {
		t.Fatalf("build: %+v %v", r, err)
	}
	return r, placeAndStart(t, f, host)
}

func withEnv(value string) apitypes.ImageDefinition {
	def := numpy()
	def.Env = &map[string]string{"IMAGE": value}
	return def
}

func (f fixture) count(t *testing.T, query string, args ...any) int {
	t.Helper()
	var n int
	if err := f.pool.QueryRow(t.Context(), query, args...).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

// queryValue is one query parameter of a URL.
func queryValue(t *testing.T, raw, key string) string {
	t.Helper()
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return u.Query().Get(key)
}

// withoutQuery is a presigned URL without its signature.
func withoutQuery(t *testing.T, raw string) string {
	t.Helper()
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	u.RawQuery = ""
	return u.String()
}

// convertReference stores a converted pair for each of contents and
// records them as reference's layers, in order. It returns their diff_ids.
func convertReference(t *testing.T, pool *pgxpool.Pool, store *storage.Storage, reference string, contents [][]byte) []imagefs.Digest {
	t.Helper()
	var out []imagefs.Digest
	for position, body := range contents {
		id := uuid.New()
		ix := storeLayer(t, store, id, layerTar(t, "file", body))
		out = append(out, ix.Layer)
		if _, err := pool.Exec(t.Context(), `insert into image_layers (id, blob_digest, diff_id, frames)
			values ($1, $2, $3, $4)`, id, "sha256:"+hex64(uuid.NewString()[:8]), ix.Layer, len(ix.Frames)); err != nil {
			t.Fatal(err)
		}
		if _, err := pool.Exec(t.Context(), `insert into image_reference_layers (reference, position, layer_id) values ($1, $2, $3)`, reference, position, id); err != nil {
			t.Fatal(err)
		}
	}
	return out
}

// hostIn inserts a host in region.
func hostIn(t *testing.T, pool *pgxpool.Pool, region string) compute.HostID {
	t.Helper()
	var id uuid.UUID
	if err := pool.QueryRow(t.Context(), `insert into hosts (name, state, cpu_millis, memory_bytes, region)
		values ('h', 'online', 8000, 1::bigint << 34, $1) returning id`, region).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return compute.HostID(id)
}

// The read URLs of a converted reference list its layers in order and read
// them by range; a reference without converted layers is a typed error.
func TestLayerReadURLs(t *testing.T) {
	ctx := t.Context()
	pool := dbtest.New(t)
	store := storage.NewStorage(pool, storagetest.Config(t))
	im := images.NewImages(pool, execution.NewExecution(pool), newVault(t, pool), store, images.Config{})

	reference := "registry.test/lazycloud/images/abc@sha256:" + hex64("1")
	contents := [][]byte{bytes.Repeat([]byte("base "), 1<<20), []byte("app layer")}
	want := convertReference(t, pool, store, reference, contents)
	host := hostIn(t, pool, "")

	reads, err := im.LayerReadURLs(ctx, reference, host, 10*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	urls := reads.Layers
	if len(urls) != len(contents) {
		t.Fatalf("got %d layers, want %d", len(urls), len(contents))
	}
	for n, u := range urls {
		if u.DiffID != want[n] {
			t.Fatalf("layer %d is %s, want %s", n, u.DiffID, want[n])
		}
		if left := time.Until(u.ExpiresAt); left < 9*time.Minute || left > 10*time.Minute {
			t.Fatalf("layer %d expires in %s", n, left)
		}
		ix := readIndex(t, u.Index)
		if ix.Layer != want[n] {
			t.Fatalf("layer %d index is of %s", n, ix.Layer)
		}
		frame, err := ix.ReadFrame(ctx, imagefs.HTTPObject(http.DefaultClient, func() string { return u.Data }), 0)
		if err != nil {
			t.Fatal(err)
		}
		e := ix.Entries[len(ix.Entries)-1]
		if end := min(e.Offset+e.Size, int64(len(frame))); !bytes.HasPrefix(contents[n], frame[e.Offset:end]) {
			t.Fatalf("layer %d reads back other bytes", n)
		}
	}

	if _, err := im.LayerReadURLs(ctx, "registry.test/lazycloud/images/abc@sha256:"+hex64("2"), host, time.Minute); !errors.Is(err, images.ErrNotConverted) {
		t.Fatalf("an unconverted reference gave %v", err)
	}
}

// An image publishes only once every layer has a pair; a second image on
// the same base converts only its own layer; repeated reports are answered
// with the same uploads.
func TestImagePublishesOnceEveryLayerIsConverted(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	host := f.host(t)
	base, err := random.Image(4096, 2)
	if err != nil {
		t.Fatal(err)
	}

	first, container := f.startBuild(t, ws, withEnv("first"), host)
	repository := f.imageRepository(t, first.Image.ID)
	img := onBase(t, base)
	digest := pushImage(t, repository+":first", img)
	reference := repository + "@" + digest

	named, err := f.images.CompleteBuild(t.Context(), host, container, images.BuildOutcome{Digest: digest})
	if err != nil || len(named) != 3 || named[0].Index != "" {
		t.Fatalf("want all three layers named for conversion, got %+v %v", named, err)
	}
	h := newFakeHost(repository)
	uploads, err := h.report(t, f, host, container, digest, named)
	if err != nil || len(uploads) != 3 || len(uploads[0].DataParts) != 1 {
		t.Fatalf("sized layers get upload URLs: %+v %v", uploads, err)
	}
	again, err := h.report(t, f, host, container, digest, named)
	if err != nil || len(again) != 3 {
		t.Fatalf("a repeated report: %d %v", len(again), err)
	}
	for n := range uploads {
		if withoutQuery(t, uploads[n].Index) != withoutQuery(t, again[n].Index) || again[n].DataParts[0] == "" ||
			queryValue(t, uploads[n].DataParts[0], "uploadId") != queryValue(t, again[n].DataParts[0], "uploadId") {
			t.Fatal("a repeated report is offered other uploads")
		}
	}

	// Two of three layers uploaded: still unpublished, and only the third
	// is offered again.
	rest, err := h.report(t, f, host, container, digest, uploads[:2])
	if err != nil || len(rest) != 1 || rest[0].Blob != uploads[2].Blob || len(rest[0].DataParts) != 1 {
		t.Fatalf("want the third layer offered, got %+v %v", rest, err)
	}
	if image, err := f.images.Get(t.Context(), ws, first.Image.ID); err != nil || image.Reference != nil {
		t.Fatalf("an image with an unconverted layer is unpublished: %+v %v", image, err)
	}
	if _, err := f.images.LayerReadURLs(t.Context(), reference, host, time.Minute); !errors.Is(err, images.ErrNotConverted) {
		t.Fatalf("nor readable: %v", err)
	}
	_, uploaded := h.answer(t, rest)
	report := images.BuildOutcome{Digest: digest, Uploaded: uploaded}
	if done, err := f.images.CompleteBuild(t.Context(), host, container, report); err != nil || len(done) != 0 {
		t.Fatalf("the last layer publishes the image: %+v %v", done, err)
	}
	if _, err := f.images.CompleteBuild(t.Context(), host, container, report); !errors.Is(err, images.ErrStaleBuild) {
		t.Fatalf("a report repeated after its answer was lost finds the build finished: %v", err)
	}
	if image, err := f.images.Get(t.Context(), ws, first.Image.ID); err != nil || image.Reference == nil || *image.Reference != reference {
		t.Fatalf("published: %+v %v", image, err)
	}
	reads, err := f.images.LayerReadURLs(t.Context(), reference, host, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	for n, d := range diffIDs(t, img) {
		if reads.Layers[n].DiffID != d {
			t.Fatalf("layer %d reads %s, want %s", n, reads.Layers[n].DiffID, d)
		}
	}

	second, container := f.startBuild(t, ws, withEnv("second"), host)
	repository = f.imageRepository(t, second.Image.ID)
	if n := f.publish(t, host, container, repository, pushImage(t, repository+":second", onBase(t, base))); n != 1 {
		t.Fatalf("an image on a converted base converts %d layers, want 1", n)
	}
	if n := f.count(t, "select count(*) from image_layers"); n != 4 {
		t.Fatalf("two images sharing two layers have %d pairs, want 4", n)
	}
	if n := f.count(t, "select count(*) from image_layer_uploads"); n != 0 {
		t.Fatalf("%d uploads are left after both published", n)
	}
}

// Two builds converting the same base layer at once end with one pair; the
// other is deleted by the sweep.
func TestConcurrentConversionsOfOneLayerEndWithOneRecord(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	host := f.host(t)
	base, err := random.Image(4096, 1)
	if err != nil {
		t.Fatal(err)
	}
	type pushed struct {
		container  execution.ContainerID
		repository string
		digest     string
		uploads    []images.LayerUpload
		host       *fakeHost
	}
	var builds []pushed
	for _, env := range []string{"one", "two"} {
		r, container := f.startBuild(t, ws, withEnv(env), host)
		repository := f.imageRepository(t, r.Image.ID)
		digest := pushImage(t, repository+":"+env, onBase(t, base))
		named, err := f.images.CompleteBuild(t.Context(), host, container, images.BuildOutcome{Digest: digest})
		if err != nil || len(named) != 2 {
			t.Fatalf("both builds convert both layers: %d %v", len(named), err)
		}
		h := newFakeHost(repository)
		uploads, err := h.report(t, f, host, container, digest, named)
		if err != nil || len(uploads) != 2 {
			t.Fatalf("both builds get uploads: %d %v", len(uploads), err)
		}
		builds = append(builds, pushed{container, repository, digest, uploads, h})
	}
	var wg sync.WaitGroup
	for _, b := range builds {
		wg.Go(func() {
			if done, err := b.host.report(t, f, host, b.container, b.digest, b.uploads); err != nil || len(done) != 0 {
				t.Errorf("publish: %+v %v", done, err)
			}
		})
	}
	wg.Wait()
	baseBlob := builds[0].uploads[0].Blob
	if n := f.count(t, "select count(*) from image_layers where blob_digest = $1", baseBlob); n != 1 {
		t.Fatalf("the shared layer has %d pairs", n)
	}
	if n := f.count(t, "select count(*) from image_reference_layers r join image_layers l on l.id = r.layer_id where l.blob_digest = $1", baseBlob); n != 2 {
		t.Fatalf("both images use the one pair: %d", n)
	}
	var loser uuid.UUID
	if err := f.pool.QueryRow(t.Context(), "select id from image_layer_uploads where blob_digest = $1 and expires_at <= now() + interval '1 hour'", baseBlob).Scan(&loser); err != nil {
		t.Fatalf("the losing upload waits for the sweep: %v", err)
	}
	// Its URLs lapse before the sweep may delete what they stored.
	if _, err := f.pool.Exec(t.Context(), "update image_layer_uploads set expires_at = now() - interval '1 second' where id = $1", loser); err != nil {
		t.Fatal(err)
	}
	if err := f.images.SweepLayers(t.Context(), slog.New(slog.DiscardHandler)); err != nil {
		t.Fatal(err)
	}
	if n := f.count(t, "select count(*) from image_layer_uploads where id = $1", loser); n != 0 {
		t.Fatal("the sweep kept the losing upload")
	}
	if _, err := f.storage.LayerSize(t.Context(), loser, storage.LayerData); !errors.Is(err, storage.ErrNotFound) {
		t.Fatalf("the losing pair is still stored: %v", err)
	}
	var winner uuid.UUID
	if err := f.pool.QueryRow(t.Context(), "select id from image_layers where blob_digest = $1", baseBlob).Scan(&winner); err != nil {
		t.Fatal(err)
	}
	if _, err := f.storage.LayerSize(t.Context(), winner, storage.LayerData); err != nil {
		t.Fatalf("the recorded pair is gone: %v", err)
	}
}

// What a customer's host converts serves only its workspace, and a pair
// that does not hold the layer its config names fails the build.
func TestCustomerHostLayersServeOnlyTheirWorkspace(t *testing.T) {
	f := newFixture(t)
	a, b := f.workspace(t, "a"), f.workspace(t, "b")
	machine := f.host(t)
	base, err := random.Image(4096, 1)
	if err != nil {
		t.Fatal(err)
	}
	r, container := f.startBuild(t, a, withEnv("customer"), machine)
	if _, err := f.pool.Exec(t.Context(), "update hosts set kind = 'machine' where id = $1", uuid.UUID(machine)); err != nil {
		t.Fatal(err)
	}
	own := f.workspaceImageRepository(t, a, r.Image.ID)
	if n := f.publish(t, machine, container, own, pushImage(t, own+":built", onBase(t, base))); n != 2 {
		t.Fatalf("converted %d layers, want 2", n)
	}
	if n := f.count(t, "select count(*) from image_layers where workspace_id = $1", uuid.UUID(a)); n != 2 {
		t.Fatalf("the customer host's pairs are its workspace's: %d", n)
	}

	platform := f.host(t)
	shared, container := f.startBuild(t, b, withEnv("platform"), platform)
	repository := f.imageRepository(t, shared.Image.ID)
	img := onBase(t, base)
	digest := pushImage(t, repository+":shared", img)
	named, err := f.images.CompleteBuild(t.Context(), platform, container, images.BuildOutcome{Digest: digest})
	if err != nil || len(named) != 2 {
		t.Fatalf("a platform build converts the base again rather than use a customer's pair: %d %v", len(named), err)
	}
	// The host converts another layer for the base blob.
	h := newFakeHost(repository)
	h.convert = func(images.LayerUpload) ([]byte, []byte) {
		var data bytes.Buffer
		ix, err := imagefs.Convert(t.Context(), bytes.NewReader(layerTar(t, "other", []byte("not the base"))), &data)
		if err != nil {
			t.Fatal(err)
		}
		encoded, err := ix.Marshal()
		if err != nil {
			t.Fatal(err)
		}
		return data.Bytes(), encoded
	}
	uploads, err := h.report(t, f, platform, container, digest, named[:1])
	if err != nil {
		t.Fatal(err)
	}
	if done, err := h.report(t, f, platform, container, digest, uploads[:1]); err != nil || len(done) != 0 {
		t.Fatalf("a wrong pair ends the build: %+v %v", done, err)
	}
	build, err := f.images.GetBuild(t.Context(), f.listener, b, shared.Build.ID, 0)
	if err != nil || build.Status != images.BuildFailed || !strings.Contains(build.Failure, "the image config names") {
		t.Fatalf("the build fails naming the mismatch: %+v %v", build, err)
	}
	if n := f.count(t, "select count(*) from image_layers where workspace_id is null"); n != 0 {
		t.Fatalf("%d pairs were recorded for everyone", n)
	}
}

// A host cannot claim more storage than a layer needs, and a failed build
// aborts the data uploads it started.
func TestLayerUploadsAreBoundedAndAbortedWithTheirBuild(t *testing.T) {
	f := newFixture(t)
	ws := f.workspace(t, "a")
	host := f.host(t)
	r, container := f.startBuild(t, ws, withEnv("bounded"), host)
	repository := f.imageRepository(t, r.Image.ID)
	digest := pushRandom(t, repository+":bounded")
	named, err := f.images.CompleteBuild(t.Context(), host, container, images.BuildOutcome{Digest: digest})
	if err != nil || len(named) != 1 {
		t.Fatalf("named: %+v %v", named, err)
	}
	h := newFakeHost(repository)
	uploads, err := h.report(t, f, host, container, digest, named)
	if err != nil || len(uploads) != 1 || len(uploads[0].DataParts) != 1 {
		t.Fatalf("uploads: %+v %v", uploads, err)
	}
	if _, err := f.images.CompleteBuild(t.Context(), host, container, images.BuildOutcome{Failure: "the upload failed"}); err != nil {
		t.Fatal(err)
	}
	// The objects stay until the index URL lapses, so a late PUT of it is
	// deleted too.
	if n := f.count(t, "select count(*) from image_layer_uploads where expires_at > now() + interval '50 minutes' and expires_at <= now() + interval '1 hour'"); n != 1 {
		t.Fatalf("%d uploads end when their URLs lapse", n)
	}
	if err := f.images.SweepLayers(t.Context(), slog.New(slog.DiscardHandler)); err != nil || f.count(t, "select count(*) from image_layer_uploads") != 1 {
		t.Fatalf("the sweep deleted an upload whose URLs still write: %v", err)
	}
	req, err := http.NewRequestWithContext(t.Context(), http.MethodPut, uploads[0].DataParts[0], bytes.NewReader(h.done[uploads[0].Blob][0]))
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode == http.StatusOK {
		t.Fatal("the failed build's data upload still takes parts")
	}

	r, container = f.startBuild(t, ws, withEnv("oversized"), host)
	repository = f.imageRepository(t, r.Image.ID)
	digest = pushRandom(t, repository+":oversized")
	named, err = f.images.CompleteBuild(t.Context(), host, container, images.BuildOutcome{Digest: digest})
	if err != nil || len(named) != 1 {
		t.Fatalf("named: %+v %v", named, err)
	}
	huge := images.BuildOutcome{Digest: digest, Converted: []images.ConvertedLayer{{Blob: named[0].Blob, DataBytes: 1 << 40, IndexBytes: 100}}}
	if done, err := f.images.CompleteBuild(t.Context(), host, container, huge); err != nil || len(done) != 0 {
		t.Fatalf("an oversized layer ends the build: %+v %v", done, err)
	}
	build, err := f.images.GetBuild(t.Context(), f.listener, ws, r.Build.ID, 0)
	if err != nil || build.Status != images.BuildFailed || !strings.Contains(build.Failure, "more than") {
		t.Fatalf("the build fails naming the size: %+v %v", build, err)
	}
}

// The sweep retires a pair only after no live image used it through the
// grace period. A replaced reference a running release still pins is live.
func TestSweepRetiresOnlyPairsNoLiveImageUses(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	ws := f.workspace(t, "a")
	logger := slog.New(slog.DiscardHandler)

	var app, workload, release uuid.UUID
	if err := f.pool.QueryRow(ctx, "insert into apps (workspace_id, name, state) values ($1, 'app', 'active') returning id", uuid.UUID(ws)).Scan(&app); err != nil {
		t.Fatal(err)
	}
	if err := f.pool.QueryRow(ctx, "insert into workloads (app_id, kind, name, desired_state) values ($1, 'function', 'f', 'active') returning id", app).Scan(&workload); err != nil {
		t.Fatal(err)
	}
	pinned := "registry.test/lazycloud/workspace-images/old@sha256:" + hex64("1")
	if err := f.pool.QueryRow(ctx, `insert into releases (workload_id, version, spec, spec_digest, source_sha256)
		values ($1, 1, jsonb_build_object('image', jsonb_build_object('reference', $2::text)), sha256('s'::bytea), sha256('s'::bytea)) returning id`,
		workload, pinned).Scan(&release); err != nil {
		t.Fatal(err)
	}
	if _, err := f.pool.Exec(ctx, "update workloads set active_release_id = $1 where id = $2", release, workload); err != nil {
		t.Fatal(err)
	}

	use := func(reference string, position int, id uuid.UUID) {
		if _, err := f.pool.Exec(ctx, "insert into image_reference_layers (reference, position, layer_id) values ($1, $2, $3)", reference, position, id); err != nil {
			t.Fatal(err)
		}
	}
	pair := func(reference string) uuid.UUID {
		id := uuid.New()
		storePair(ctx, t, f.storage, id, []byte("pair"), []byte("pair"))
		if _, err := f.pool.Exec(ctx, `insert into image_layers (id, blob_digest, diff_id, frames)
			values ($1, $2, $2, 0)`, id, "sha256:"+strings.Repeat(strings.ReplaceAll(id.String(), "-", ""), 2)); err != nil {
			t.Fatal(err)
		}
		use(reference, 0, id)
		return id
	}
	gone := "registry.test/lazycloud/workspace-images/gone@sha256:" + hex64("2")
	live := pair(pinned)
	stale := pair(gone)
	// gone shares its second layer with the pinned reference.
	shared := pair("registry.test/lazycloud/workspace-images/shared@sha256:" + hex64("6"))
	use(gone, 1, shared)
	use(pinned, 1, shared)
	recent := pair("registry.test/lazycloud/workspace-images/recent@sha256:" + hex64("3"))
	startedRef := "registry.test/lazycloud/images/started@sha256:" + hex64("4")
	started := pair(startedRef)
	if err := f.images.RecordUses(ctx, []string{startedRef, startedRef}); err != nil {
		t.Fatal(err)
	}
	if _, err := f.pool.Exec(ctx, "insert into image_reference_uses (reference, used_at) values ('old', now() - interval '25 hours')"); err != nil {
		t.Fatal(err)
	}
	// A published image no live release pins and no host started is not
	// live: image history is not read.
	published := "registry.test/lazycloud/images/published@sha256:" + hex64("5")
	idle := pair(published)
	if _, err := f.pool.Exec(ctx, `insert into images (digest, id, dockerfile, python_version, architecture, reference, ready_at)
		values (sha256('published'::bytea), 'img_' || left(encode(sha256('published'::bytea), 'hex'), 24), 'FROM x', '3.12', 'amd64', $1, now())`, published); err != nil {
		t.Fatal(err)
	}

	if err := f.images.SweepLayers(ctx, logger); err != nil {
		t.Fatal(err)
	}
	if n := f.count(t, "select count(*) from image_layers where unreferenced_since is not null and id = any($1)", []uuid.UUID{stale, recent, idle}); n != 3 ||
		f.count(t, "select count(*) from image_layers where unreferenced_since is not null") != 3 {
		t.Fatalf("pairs in their grace period: want stale, recent and idle only")
	}
	if f.count(t, "select count(*) from image_reference_uses where reference = 'old'") != 0 {
		t.Fatal("a use older than the grace period stays")
	}
	if f.count(t, "select count(*) from image_layers where id = any($1) and unreferenced_since is null", []uuid.UUID{live, shared, started}) != 3 {
		t.Fatal("a pinned, shared or started pair started its grace period")
	}
	// stale has been unused past the grace period; recent only just.
	if _, err := f.pool.Exec(ctx, "update image_layers set unreferenced_since = now() - interval '25 hours' where id = $1", stale); err != nil {
		t.Fatal(err)
	}
	if err := f.images.SweepLayers(ctx, logger); err != nil {
		t.Fatal(err)
	}
	if n := f.count(t, "select count(*) from image_layers where id = $1", stale); n != 0 || f.count(t, "select count(*) from image_layer_uploads") != 0 {
		t.Fatal("the stale pair was not retired and deleted")
	}
	if n := f.count(t, "select count(*) from image_layers where id = any($1)", []uuid.UUID{live, recent}); n != 2 {
		t.Fatal("a live or recently used pair was retired")
	}
	if _, err := f.storage.LayerSize(ctx, stale, storage.LayerIndex); !errors.Is(err, storage.ErrNotFound) {
		t.Fatalf("the retired pair is still stored: %v", err)
	}
	if n := f.count(t, "select count(*) from image_reference_layers where reference = $1", gone); n != 0 {
		t.Fatal("a reference that used the retired pair keeps some of its layers")
	}
	if n := f.count(t, "select count(*) from image_reference_layers where reference = $1", pinned); n != 2 {
		t.Fatalf("the pinned reference has %d layers, want 2", n)
	}

	// A deploy that pins recent's reference again ends its grace period.
	if _, err := f.pool.Exec(ctx, `update releases set spec = jsonb_build_object('image', jsonb_build_object('reference',
		'registry.test/lazycloud/workspace-images/recent@sha256:' || $2::text)) where id = $1`, release, hex64("3")); err != nil {
		t.Fatal(err)
	}
	if err := f.images.SweepLayers(ctx, logger); err != nil {
		t.Fatal(err)
	}
	if n := f.count(t, "select count(*) from image_layers where id = $1 and unreferenced_since is null", recent); n != 1 {
		t.Fatal("a pair used again is still in its grace period")
	}
}

func readIndex(t *testing.T, target string) imagefs.Index {
	t.Helper()
	req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, target, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	b, err := io.ReadAll(resp.Body)
	if err != nil || resp.StatusCode != http.StatusOK {
		t.Fatalf("read index: %s %v", resp.Status, err)
	}
	ix, err := imagefs.Unmarshal(b)
	if err != nil {
		t.Fatal(err)
	}
	return ix
}

// hex64 is a 64-character hex string made of seed repeated.
func hex64(seed string) string {
	return strings.Repeat(seed, 64)[:64]
}
