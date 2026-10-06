package hostsession_test

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/hostsession"
	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// storedLayer is a converted layer pair in the store and its row.
type storedLayer struct {
	id     uuid.UUID
	diffID imagefs.Digest
	index  []byte
}

// storeLayer converts a one-file layer, uploads its pair and records it.
func (h *harness) storeLayer(contents string) storedLayer {
	h.t.Helper()
	var layer bytes.Buffer
	tw := tar.NewWriter(&layer)
	if err := tw.WriteHeader(&tar.Header{Name: "file", Mode: 0o644, Size: int64(len(contents)), Typeflag: tar.TypeReg}); err != nil {
		h.t.Fatal(err)
	}
	if _, err := io.WriteString(tw, contents); err != nil {
		h.t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		h.t.Fatal(err)
	}
	var data bytes.Buffer
	ix, err := imagefs.Convert(h.t.Context(), &layer, &data)
	if err != nil {
		h.t.Fatal(err)
	}
	index, err := ix.Marshal()
	if err != nil {
		h.t.Fatal(err)
	}
	l := storedLayer{id: uuid.New(), diffID: ix.Layer, index: index}
	upload, err := h.store.CreateLayerUpload(h.t.Context(), l.id, int64(data.Len()))
	if err != nil {
		h.t.Fatal(err)
	}
	urls, err := h.store.PresignLayerUpload(h.t.Context(), l.id, upload, int64(data.Len()), int64(len(index)), time.Minute)
	if err != nil {
		h.t.Fatal(err)
	}
	put := func(target string, body []byte) string {
		req, err := http.NewRequestWithContext(h.t.Context(), http.MethodPut, target, bytes.NewReader(body))
		if err != nil {
			h.t.Fatal(err)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			h.t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			h.t.Fatalf("upload: %s", resp.Status)
		}
		return resp.Header.Get("ETag")
	}
	put(urls.Index, index)
	etags := make([]string, len(urls.DataParts))
	for n, part := range urls.DataParts {
		body := data.Bytes()[int64(n)*storage.LayerPartBytes : min(int64(n+1)*storage.LayerPartBytes, int64(data.Len()))]
		etags[n] = put(part, body)
	}
	if err := h.store.CompleteLayerUpload(h.t.Context(), l.id, upload, etags); err != nil {
		h.t.Fatal(err)
	}
	if _, err := h.pool.Exec(h.t.Context(), `insert into image_layers (id, blob_digest, diff_id, frames)
		values ($1, $2, $3, $4)`, l.id, randomDigest(), ix.Layer, len(ix.Frames)); err != nil {
		h.t.Fatal(err)
	}
	return l
}

func randomDigest() string {
	b := make([]byte, 32)
	_, _ = rand.Read(b)
	return "sha256:" + hex.EncodeToString(b)
}

// publish records reference as an image published with layers, base first.
func (h *harness) publish(reference string, layers ...storedLayer) {
	h.t.Helper()
	for n, l := range layers {
		if _, err := h.pool.Exec(h.t.Context(), `insert into image_reference_layers (reference, position, layer_id) values ($1, $2, $3)`,
			reference, n, l.id); err != nil {
			h.t.Fatal(err)
		}
	}
}

// startingImage inserts an image and a starting container on host of a
// release that pinned reference for it.
func (h *harness) startingImage(host compute.HostID, reference string) uuid.UUID {
	h.t.Helper()
	sum := sha256.Sum256([]byte(reference))
	id := "img_" + hex.EncodeToString(sum[:12])
	if _, err := h.pool.Exec(h.t.Context(), `insert into images (digest, id, dockerfile, python_version, architecture, reference, ready_at)
		values ($1, $2, 'FROM scratch', '3.12', 'amd64', $3, now()) on conflict do nothing`, sum[:], id, reference); err != nil {
		h.t.Fatal(err)
	}
	_, container := h.startingWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.12", "image_id": "`+id+`", "reference": "`+reference+`"}}`)
	return container
}

func reference(name string) string {
	sum := sha256.Sum256([]byte(name + uuid.NewString()))
	return "registry.test/lazycloud/images/" + name + "@sha256:" + hex.EncodeToString(sum[:])
}

// commands forwards every command on stream for the test's lifetime.
func commands(t *testing.T, stream hostStream) <-chan *hostproto.ServerMessage {
	t.Helper()
	out := make(chan *hostproto.ServerMessage, 64)
	go func() {
		defer close(out)
		for {
			msg, err := stream.Recv()
			if err != nil {
				return
			}
			out <- msg
		}
	}()
	return out
}

// next returns the first command match accepts, failing after timeout.
func next(t *testing.T, in <-chan *hostproto.ServerMessage, timeout time.Duration, match func(*hostproto.ServerMessage) bool) *hostproto.ServerMessage {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case msg, ok := <-in:
			if !ok {
				t.Fatal("session ended")
			}
			if match(msg) {
				return msg
			}
		case <-deadline:
			t.Fatal("no matching command in time")
			return nil
		}
	}
}

func diffIDs(grants []*hostproto.LayerGrant) []string {
	out := make([]string, len(grants))
	for n, g := range grants {
		out[n] = g.GetDiffId()
	}
	return out
}

func get(t *testing.T, target string, header http.Header) (int, []byte) {
	t.Helper()
	req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, target, nil)
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range header {
		req.Header[k] = v
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return resp.StatusCode, body
}

// TestStartCarriesOnlyItsImageLayers: a start carries reads of exactly the
// layers of the image its release pinned, base first; the host learns
// nothing of another image's layers, even one sharing its base; replicas
// of one image share one signing; the managed Python image carries its own
// layers; a start whose image has no converted layers waits for the build
// that converts it.
func TestStartCarriesOnlyItsImageLayers(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	other, _ := h.enroll()
	base, appA, appB := h.storeLayer("base"), h.storeLayer("app a"), h.storeLayer("app b")
	refA, refB := reference("a"), reference("b")
	h.publish(refA, base, appA)
	h.publish(refB, base, appB)

	mine := h.startingImage(host, refA)
	replica := h.startingImage(host, refA)
	h.startingImage(other, refB)
	old := reference("old")
	unconverted := h.startingImage(host, old)
	_, managed := h.startingWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.12"}}`)

	in := commands(t, open(t, ctx, h.client))
	starts := map[string]*hostproto.StartContainer{}
	for len(starts) < 3 {
		msg := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart() != nil || m.GetLayerGrants() != nil })
		if msg.GetLayerGrants() != nil {
			t.Fatalf("the host got grants outside a start: %v", diffIDs(msg.GetLayerGrants().GetLayers()))
		}
		starts[msg.GetStart().GetContainerId()] = msg.GetStart()
	}
	got := starts[mine.String()]
	// Replicas of one image share one signing of its layers.
	if r := starts[replica.String()]; r == nil || len(r.GetLayers()) != 2 ||
		!r.GetLayers()[0].GetExpiresAt().AsTime().Equal(got.GetLayers()[0].GetExpiresAt().AsTime()) {
		t.Fatalf("replica start %v", r.GetLayers())
	}
	if want := []string{string(base.diffID), string(appA.diffID)}; got == nil || !slices.Equal(diffIDs(got.GetLayers()), want) {
		t.Fatalf("start of image a carries %v, want %v", diffIDs(got.GetLayers()), want)
	}
	if !strings.Contains(got.GetLayers()[1].GetIndexUrl(), appA.id.String()) || strings.Contains(got.GetLayers()[1].GetIndexUrl(), appB.id.String()) {
		t.Fatalf("app layer URL %s", got.GetLayers()[1].GetIndexUrl())
	}
	if m := starts[managed.String()]; m == nil || m.GetImage() != managedReference("3.12") || len(m.GetLayers()) != 1 {
		t.Fatalf("managed image start %v", m)
	}
	if _, sent := starts[unconverted.String()]; sent {
		t.Fatal("an image without converted layers was started")
	}
	var state string
	var conversions int
	if err := h.pool.QueryRow(t.Context(), `select state, (select count(*) from image_builds b join images i on i.digest = b.image_digest
		where i.dockerfile = 'FROM ' || $2 || E'\n' and b.state = 'building') from containers where id = $1`, unconverted, old).
		Scan(&state, &conversions); err != nil {
		t.Fatal(err)
	}
	if state != "starting" || conversions != 1 {
		t.Fatalf("a container waiting for its image's layers is %s with %d conversion builds", state, conversions)
	}
}

// TestLayerGrantsOpenOnlyTheirObjectAndRefreshWhileUsed: a granted URL reads
// its own object and no other, stops working at expiry, and the host gets
// fresh grants before then for as long as a container uses the image, then
// no more.
func TestLayerGrantsOpenOnlyTheirObjectAndRefreshWhileUsed(t *testing.T) {
	h := start(t)
	const lifetime = 3 * time.Second
	hostsession.SetLayerLifetime(h.server, lifetime)
	host, ctx := h.enroll()
	layer, other := h.storeLayer("granted"), h.storeLayer("not granted")
	ref := reference("a")
	h.publish(ref, layer)
	h.publish(reference("b"), other)
	container := h.startingImage(host, ref)

	in := commands(t, open(t, ctx, h.client))
	first := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart() != nil }).GetStart().GetLayers()[0]

	status, body := get(t, first.GetIndexUrl(), nil)
	if status != http.StatusOK || !bytes.Equal(body, layer.index) {
		t.Fatalf("granted index: %d", status)
	}
	if status, _ := get(t, first.GetDataUrl(), http.Header{"Range": {"bytes=0-3"}}); status != http.StatusPartialContent {
		t.Fatalf("range read of granted data: %d", status)
	}
	// The signature covers the key: the same query on another object's key,
	// or on the pair's other object, is refused.
	swapped := map[string]string{
		"another layer's index": strings.Replace(first.GetIndexUrl(), layer.id.String(), other.id.String(), 1),
		"another layer's data":  strings.Replace(first.GetDataUrl(), layer.id.String(), other.id.String(), 1),
		"the pair's data":       strings.Replace(first.GetIndexUrl(), "/index?", "/data?", 1),
	}
	for name, target := range swapped {
		if target == first.GetIndexUrl() || target == first.GetDataUrl() {
			t.Fatalf("%s: the URL did not change", name)
		}
		if status, _ := get(t, target, nil); status != http.StatusForbidden {
			t.Fatalf("%s with a granted signature: %d", name, status)
		}
	}
	if u, _ := url.Parse(first.GetIndexUrl()); u.Query().Get("X-Amz-Signature") == "" {
		t.Fatalf("index URL is not presigned: %s", first.GetIndexUrl())
	}

	// A fresh grant arrives at half life, before the first expires.
	refresh := next(t, in, lifetime, func(m *hostproto.ServerMessage) bool { return m.GetLayerGrants() != nil }).GetLayerGrants().GetLayers()
	if !slices.Equal(diffIDs(refresh), []string{string(layer.diffID)}) {
		t.Fatalf("refresh names %v", diffIDs(refresh))
	}
	if !refresh[0].GetExpiresAt().AsTime().After(first.GetExpiresAt().AsTime()) || !time.Now().Before(first.GetExpiresAt().AsTime()) {
		t.Fatalf("refresh expires %s, first %s", refresh[0].GetExpiresAt().AsTime(), first.GetExpiresAt().AsTime())
	}
	time.Sleep(time.Until(first.GetExpiresAt().AsTime().Add(1500 * time.Millisecond)))
	if status, _ := get(t, first.GetIndexUrl(), nil); status == http.StatusOK {
		t.Fatal("an expired URL still reads")
	} else {
		t.Logf("expired URL answered %d", status)
	}
	// The host keeps reading past the first expiry with what it was sent.
	latest := next(t, in, lifetime, func(m *hostproto.ServerMessage) bool {
		layers := m.GetLayerGrants().GetLayers()
		return len(layers) > 0 && time.Until(layers[0].GetExpiresAt().AsTime()) > time.Second
	}).GetLayerGrants().GetLayers()[0]
	if status, body := get(t, latest.GetIndexUrl(), nil); status != http.StatusOK || !bytes.Equal(body, layer.index) {
		t.Fatalf("refreshed index after the first expired: %d", status)
	}

	// Once the container stops, grants stop.
	if _, err := h.pool.Exec(t.Context(), `update containers set state = 'stopped', stop_reason = 'stopped', stopped_at = now() where id = $1`, container); err != nil {
		t.Fatal(err)
	}
	next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStop().GetContainerId() == container.String() })
	quiet := time.After(2 * lifetime)
	for {
		select {
		case msg := <-in:
			if msg.GetLayerGrants() != nil {
				t.Fatal("grants continued after the image's last container stopped")
			}
		case <-quiet:
			return
		}
	}
}

// TestReconnectedSessionGrantsRunningImages: a session that opens on a host
// already running a container sends grants for the image it was started
// with, the managed Python image included, and none for an image only a
// stopped container used.
func TestReconnectedSessionGrantsRunningImages(t *testing.T) {
	h := start(t)
	host, ctx := h.enroll()
	_, managed := h.startingWith(host, `{"handler": "reports:summarize", "image": {"python_version": "3.12"}}`)
	layer := h.storeLayer("stopped image")
	ref := reference("stopped")
	h.publish(ref, layer)
	stopped := h.startingImage(host, ref)

	first, closeFirst := context.WithCancel(ctx)
	in := commands(t, open(t, first, h.client))
	for range 2 {
		next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart() != nil })
	}
	closeFirst()
	if _, err := h.pool.Exec(t.Context(), `update containers set state = 'ready', ready_at = now() where id = $1`, managed); err != nil {
		t.Fatal(err)
	}
	if _, err := h.pool.Exec(t.Context(), `update containers set state = 'stopped', stop_reason = 'stopped', stopped_at = now() where id = $1`, stopped); err != nil {
		t.Fatal(err)
	}

	in = commands(t, open(t, ctx, h.client, &hostproto.ContainerReport{
		ContainerId: managed.String(), Phase: hostproto.ContainerPhase_CONTAINER_PHASE_READY, ObservedAt: timestamppb.Now(),
	}))
	grants := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetLayerGrants() != nil }).GetLayerGrants().GetLayers()
	if len(grants) != 1 || grants[0].GetDiffId() == string(layer.diffID) {
		t.Fatalf("the reopened session granted %v", diffIDs(grants))
	}
	quiet := time.After(time.Second)
	for {
		select {
		case msg := <-in:
			if msg.GetLayerGrants() != nil {
				t.Fatalf("the reopened session also granted %v", diffIDs(msg.GetLayerGrants().GetLayers()))
			}
		case <-quiet:
			return
		}
	}
}

// bucketOf is the bucket a path-style presigned URL reads.
func bucketOf(t *testing.T, raw string) string {
	t.Helper()
	u, err := url.Parse(raw)
	if err != nil {
		t.Fatal(err)
	}
	return strings.SplitN(strings.TrimPrefix(u.Path, "/"), "/", 2)[0]
}

// A start on a host in a region with a copy of the layer bucket reads the
// layer bucket while the copy is unconfirmed. A layer replicated after the
// server's first check is found by its next one, and the host gets grants
// from the copy then, not when its hour-long grant is due. Garage signs one
// region, so the platform bucket stands in for that region's copy.
func TestALateCopyIsGrantedOnceTheRecheckFindsIt(t *testing.T) {
	cfg := storagetest.Config(t)
	cfg.LayerReplicas = map[string]string{cfg.Region: cfg.Bucket}
	h := serveWith(t, dbtest.New(t), cfg)
	const recheck = 500 * time.Millisecond
	hostsession.SetReplicaRecheck(h.server, recheck)
	host, ctx := h.enroll()
	h.exec("update hosts set region = $1 where id = $2", cfg.Region, uuid.UUID(host))
	layer := h.storeLayer("regional")
	ref := reference("regional")
	h.publish(ref, layer)
	h.startingImage(host, ref)

	in := commands(t, open(t, ctx, h.client))
	first := next(t, in, 5*time.Second, func(m *hostproto.ServerMessage) bool { return m.GetStart() != nil }).GetStart().GetLayers()[0]
	if bucketOf(t, first.GetIndexUrl()) != cfg.LayerBucket || bucketOf(t, first.GetDataUrl()) != cfg.LayerBucket {
		t.Fatalf("an unconfirmed copy was granted: %s", first.GetIndexUrl())
	}
	for h.count("select count(*) from image_layer_replicas where confirmed_at is null") == 0 {
		time.Sleep(10 * time.Millisecond)
	}
	client := s3.New(s3.Options{
		Region: cfg.Region, BaseEndpoint: aws.String(cfg.Endpoint), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(cfg.AccessKeyID, cfg.SecretAccessKey, ""),
	})
	for _, object := range []string{"index", "data"} {
		key := "layers/" + layer.id.String() + "/" + object
		if _, err := client.CopyObject(t.Context(), &s3.CopyObjectInput{
			Bucket: aws.String(cfg.Bucket), Key: aws.String(key), CopySource: aws.String(cfg.LayerBucket + "/" + key),
		}); err != nil {
			t.Fatal(err)
		}
	}
	copied := next(t, in, 10*recheck, func(m *hostproto.ServerMessage) bool {
		layers := m.GetLayerGrants().GetLayers()
		return len(layers) > 0 && bucketOf(t, layers[0].GetIndexUrl()) == cfg.Bucket
	}).GetLayerGrants().GetLayers()[0]
	if bucketOf(t, copied.GetDataUrl()) != cfg.Bucket {
		t.Fatalf("the copy's grant reads data from %s", copied.GetDataUrl())
	}
	if status, body := get(t, copied.GetIndexUrl(), nil); status != http.StatusOK || !bytes.Equal(body, layer.index) {
		t.Fatalf("the copy's index: %d", status)
	}
}
