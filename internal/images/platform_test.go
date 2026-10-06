package images_test

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/go-containerregistry/pkg/v1/empty"
	"github.com/google/go-containerregistry/pkg/v1/mutate"
	"github.com/google/go-containerregistry/pkg/v1/random"
	"github.com/google/go-containerregistry/pkg/v1/static"
	"github.com/google/go-containerregistry/pkg/v1/types"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/imagefs"
	"github.com/AmbientWare/lazycloud/internal/images"
)

func (f fixture) replica() *images.Images {
	return images.NewImages(f.pool, f.execution, f.secrets, f.storage, images.Config{
		Registry: f.registry, Repository: "lazycloud", Insecure: true, ManagedBase: f.registry + "/library/python:{version}-slim",
	})
}

// A platform image waits while another replica's conversion holds its
// lease, converts once however many replicas are asked at once, and is
// then pulled from its copy in the platform registry with every layer
// readable. A later image on its base converts only its own layer.
func TestPlatformImagesConvertOnceAcrossReplicas(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	host := f.host(t)
	img, err := random.Image(4096, 2)
	if err != nil {
		t.Fatal(err)
	}
	digest := pushImage(t, f.registry+"/tools/builder:1", img)
	reference := f.registry + "/tools/builder:1@" + digest

	if _, err := f.pool.Exec(ctx, `insert into platform_images (reference, architecture, lease_token, leased_until)
		values ($1, 'amd64', gen_random_uuid(), now() + interval '1 hour')`, reference); err != nil {
		t.Fatal(err)
	}
	if err := f.images.ConvertPlatformImage(ctx, reference, "amd64"); err != nil {
		t.Fatal(err)
	}
	pulls, err := f.images.PlatformPulls(ctx, host, []string{reference})
	if err != nil || pulls[0].Pull != nil || pulls[0].Failure != "" || pulls[0].Architecture != "amd64" {
		t.Fatalf("an image another replica converts waits: %+v %v", pulls, err)
	}
	if n := f.count(t, "select count(*) from image_layers"); n != 0 {
		t.Fatalf("a held lease converted %d layers", n)
	}

	// The owner's lease lapsed, as when its server died: the requests that
	// follow convert the image once between them.
	if _, err := f.pool.Exec(ctx, "update platform_images set leased_until = now() - interval '1 second'"); err != nil {
		t.Fatal(err)
	}
	replicas := []*images.Images{f.images, f.replica()}
	var wg sync.WaitGroup
	for n := range 6 {
		wg.Go(func() {
			if err := replicas[n%2].ConvertPlatformImage(ctx, reference, "amd64"); err != nil {
				t.Errorf("convert: %v", err)
			}
		})
	}
	wg.Wait()
	mirror := f.registry + "/lazycloud/platform/" + strings.ReplaceAll(f.registry, ":", "-") + "/tools/builder@" + digest
	for _, im := range replicas {
		pulls, err := im.PlatformPulls(ctx, host, []string{reference})
		if err != nil || pulls[0].Pull == nil || pulls[0].Pull.Reference != mirror || pulls[0].Pull.Platform != "linux/amd64" {
			t.Fatalf("a converted platform image pulls its copy %s: %+v %v", mirror, pulls, err)
		}
	}
	if n := f.count(t, "select count(*) from image_layers where workspace_id is null"); n != 2 {
		t.Fatalf("%d shared pairs, want 2", n)
	}
	if n := f.count(t, "select count(*) from image_layer_uploads"); n != 0 {
		t.Fatalf("%d uploads left: a second conversion ran", n)
	}
	if n := f.count(t, "select count(*) from platform_images where lease_token is null and failure is null"); n != 1 {
		t.Fatal("the conversion's lease and failure stayed")
	}
	if n := f.count(t, "select count(*) from image_reference_uses where reference = $1", mirror); n != 1 {
		t.Fatal("the converted copy is not live for the layer sweep")
	}
	reads, err := f.images.LayerReadURLs(ctx, mirror, host, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	var got []imagefs.Digest
	for _, u := range reads.Layers {
		_, ix, err := imagefs.FetchIndex(ctx, http.DefaultClient, u.Index)
		if err != nil {
			t.Fatal(err)
		}
		got = append(got, ix.Layer)
	}
	if want := diffIDs(t, img); !slices.Equal(got, want) {
		t.Fatalf("the copy's pairs hold %v, want %v", got, want)
	}

	derived := onBase(t, img)
	derivedDigest := pushImage(t, f.registry+"/tools/mount:1", derived)
	if err := f.images.ConvertPlatformImage(ctx, f.registry+"/tools/mount:1@"+derivedDigest, "amd64"); err != nil {
		t.Fatal(err)
	}
	if n := f.count(t, "select count(*) from image_layers"); n != 3 {
		t.Fatalf("an image on a converted base made %d pairs in all, want 3", n)
	}
}

// An image whose layer cannot be converted fails with its reason, which
// requests get without converting again until the retry period passes. An
// unreachable registry says nothing of the image: requests keep waiting and
// the conversion runs again after a short delay. A reference that is not
// a public image by digest is refused outright.
func TestPlatformImageFailuresAreTypedAndRetried(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	host := f.host(t)
	// A hard link to a file the layer does not hold.
	var layer bytes.Buffer
	tw := tar.NewWriter(&layer)
	if err := tw.WriteHeader(&tar.Header{Name: "b", Typeflag: tar.TypeLink, Linkname: "a", Mode: 0o644}); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	img, err := mutate.AppendLayers(empty.Image, static.NewLayer(layer.Bytes(), types.DockerUncompressedLayer))
	if err != nil {
		t.Fatal(err)
	}
	broken := f.registry + "/tools/broken:1@" + pushImage(t, f.registry+"/tools/broken:1", img)
	failedAt := func(reference string) time.Time {
		var at time.Time
		if err := f.pool.QueryRow(ctx, "select failed_at from platform_images where reference = $1", reference).Scan(&at); err != nil {
			t.Fatal(err)
		}
		return at
	}

	var unconvertible *images.ConversionError
	if err := f.images.ConvertPlatformImage(ctx, broken, "amd64"); !errors.As(err, &unconvertible) {
		t.Fatalf("a layer conversion refuses fails the image: %v", err)
	}
	first := failedAt(broken)
	pulls, err := f.images.PlatformPulls(ctx, host, []string{broken})
	if err != nil || pulls[0].Failure == "" || pulls[0].Pull != nil {
		t.Fatalf("requests get the failure: %+v %v", pulls, err)
	}
	if err := f.images.ConvertPlatformImage(ctx, broken, "amd64"); err != nil || !failedAt(broken).Equal(first) {
		t.Fatalf("a failed image converted again within its retry period: %v", err)
	}
	if _, err := f.pool.Exec(ctx, "update platform_images set failed_at = now() - interval '11 minutes' where reference = $1", broken); err != nil {
		t.Fatal(err)
	}
	if err := f.images.ConvertPlatformImage(ctx, broken, "amd64"); !errors.As(err, &unconvertible) || !failedAt(broken).After(first) {
		t.Fatalf("past its retry period the image is tried again: %v", err)
	}

	dead := images.NewImages(f.pool, f.execution, f.secrets, f.storage, images.Config{Registry: "127.0.0.1:1", Repository: "lazycloud", Insecure: true})
	unreachable := "127.0.0.1:1/tools/builder@sha256:" + strings.Repeat("e", 64)
	if err := dead.ConvertPlatformImage(ctx, unreachable, "amd64"); errors.As(err, &unconvertible) || !errors.Is(err, images.ErrRegistryUnavailable) {
		t.Fatalf("an unreachable registry is a transient failure: %v", err)
	}
	pulls, err = dead.PlatformPulls(ctx, host, []string{unreachable})
	if err != nil || pulls[0].Failure != "" || pulls[0].Pull != nil {
		t.Fatalf("requests wait through a transient failure: %+v %v", pulls, err)
	}
	if n := f.count(t, "select count(*) from platform_images where reference = $1 and failure_transient and leased_until is null", unreachable); n != 1 {
		t.Fatal("a transient failure is recorded and ends its lease")
	}
	first = failedAt(unreachable)
	if err := dead.ConvertPlatformImage(ctx, unreachable, "amd64"); err != nil || !failedAt(unreachable).Equal(first) {
		t.Fatalf("a transient failure is tried again at once: %v", err)
	}
	if _, err := f.pool.Exec(ctx, "update platform_images set failed_at = now() - interval '31 seconds' where reference = $1", unreachable); err != nil {
		t.Fatal(err)
	}
	if err := dead.ConvertPlatformImage(ctx, unreachable, "amd64"); err == nil || !failedAt(unreachable).After(first) {
		t.Fatalf("after its delay a transient failure is tried again: %v", err)
	}

	refused := []string{
		"docker.io/library/busybox:latest",
		"10.0.0.1/tools/builder@sha256:" + strings.Repeat("a", 64),
		f.registry + "/lazycloud/images/" + strings.Repeat("b", 64) + "@sha256:" + strings.Repeat("b", 64),
	}
	pulls, err = f.images.PlatformPulls(ctx, host, refused)
	if err != nil {
		t.Fatal(err)
	}
	for n, p := range pulls {
		if p.Failure == "" {
			t.Errorf("%s is refused: %+v", refused[n], p)
		}
		if err := f.images.ConvertPlatformImage(ctx, refused[n], "amd64"); err == nil {
			t.Errorf("%s converts", refused[n])
		}
	}
	if n := f.count(t, "select count(*) from platform_images where reference = any($1)", refused); n != 0 {
		t.Fatalf("refused references left %d rows", n)
	}
}

// A server whose temporary directory cannot be written fails the
// conversion as ErrServerDisk, not for the image: hosts get the reason, and
// the conversion waits the long retry period instead of running again
// every few seconds. Once the directory is writable it converts.
func TestAServerWithoutWritableTempFailsItsConversionsVisibly(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	host := f.host(t)
	img, err := random.Image(4096, 1)
	if err != nil {
		t.Fatal(err)
	}
	reference := f.registry + "/tools/builder:disk@" + pushImage(t, f.registry+"/tools/builder:disk", img)
	tmp := os.Getenv("TMPDIR")
	t.Setenv("TMPDIR", filepath.Join(t.TempDir(), "missing"))
	err = f.images.ConvertPlatformImage(ctx, reference, "amd64")
	var unconvertible *images.ConversionError
	if !errors.Is(err, images.ErrServerDisk) || errors.As(err, &unconvertible) {
		t.Fatalf("an unwritable temporary directory is the server's disk failure: %v", err)
	}
	pulls, err := f.images.PlatformPulls(ctx, host, []string{reference})
	if err != nil || !strings.Contains(pulls[0].Failure, images.ErrServerDisk.Error()) || pulls[0].RetryAt.Before(time.Now().Add(9*time.Minute)) {
		t.Fatalf("hosts get the disk failure until the long retry period passes: %+v %v", pulls, err)
	}
	if _, err := f.pool.Exec(ctx, "update platform_images set failed_at = now() - interval '31 seconds' where reference = $1", reference); err != nil {
		t.Fatal(err)
	}
	if err := f.images.ConvertPlatformImage(ctx, reference, "amd64"); err != nil {
		t.Fatal(err)
	}
	if n := f.count(t, "select count(*) from platform_images where reference = $1 and failure is not null", reference); n != 1 {
		t.Fatal("a disk failure was converted again within the short retry period")
	}
	t.Setenv("TMPDIR", tmp)
	if _, err := f.pool.Exec(ctx, "update platform_images set failed_at = now() - interval '11 minutes' where reference = $1", reference); err != nil {
		t.Fatal(err)
	}
	if err := f.images.ConvertPlatformImage(ctx, reference, "amd64"); err != nil {
		t.Fatal(err)
	}
	if pulls, err := f.images.PlatformPulls(ctx, host, []string{reference}); err != nil || pulls[0].Pull == nil {
		t.Fatalf("a writable directory converts the image: %+v %v", pulls, err)
	}
}

// hangingRegistry answers no request until the client gives up, so a
// conversion from it runs until its context ends.
func hangingRegistry(t *testing.T) string {
	t.Helper()
	done := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		select {
		case <-r.Context().Done():
		case <-done:
		}
	}))
	t.Cleanup(func() { close(done); server.Close() })
	return strings.TrimPrefix(server.URL, "http://")
}

// A conversion renews its lease while it runs, so no replica takes it
// over; the owner whose lease another took stops. A lease its owner no
// longer renews, as when its server died, is taken over once it lapses.
func TestPlatformLeasesAreRenewedAndTakenOverFromACrashedOwner(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	registry := hangingRegistry(t)
	replica := func() *images.Images {
		im := images.NewImages(f.pool, f.execution, f.secrets, f.storage, images.Config{Registry: registry, Repository: "lazycloud", Insecure: true})
		images.SetPlatformLease(im, 2*time.Second)
		return im
	}
	owner, other := replica(), replica()
	reference := registry + "/tools/builder@sha256:" + strings.Repeat("c", 64)
	// A replica that claims the lease converts until its attempt ends, and
	// fails; one that finds the lease held returns at once.
	try := func() error {
		attempt, cancel := context.WithTimeout(ctx, time.Second)
		defer cancel()
		return other.ConvertPlatformImage(attempt, reference, "amd64")
	}
	token := func() string {
		var token *string
		err := f.pool.QueryRow(ctx, "select lease_token::text from platform_images where reference = $1", reference).Scan(&token)
		if errors.Is(err, pgx.ErrNoRows) || token == nil {
			return ""
		}
		if err != nil {
			t.Fatal(err)
		}
		return *token
	}

	ended := make(chan error, 1)
	go func() { ended <- owner.ConvertPlatformImage(ctx, reference, "amd64") }()
	deadline := time.Now().Add(5 * time.Second)
	for token() == "" {
		if time.Now().After(deadline) {
			t.Fatal("the conversion took no lease")
		}
		time.Sleep(20 * time.Millisecond)
	}
	held := token()
	time.Sleep(5 * time.Second)
	if err := try(); err != nil || token() != held {
		t.Fatalf("a running conversion's lease passed to another replica past its length: %v", err)
	}

	// Another owner takes the lease, then dies without renewing it.
	if _, err := f.pool.Exec(ctx, "update platform_images set lease_token = gen_random_uuid(), leased_until = now() + interval '2 seconds' where reference = $1", reference); err != nil {
		t.Fatal(err)
	}
	crashed := token()
	select {
	case err := <-ended:
		if err == nil {
			t.Fatal("the owner that lost its lease converted")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the owner kept converting after another took its lease")
	}
	if err := try(); err != nil || token() != crashed {
		t.Fatalf("a live lease was taken: %v", err)
	}
	time.Sleep(2500 * time.Millisecond)
	if err := try(); err == nil {
		t.Fatal("the replica that took the lapsed lease did not convert")
	}
	if n := f.count(t, "select count(*) from platform_images where reference = $1 and lease_token is null and failure_transient", reference); n != 1 {
		t.Fatal("the lapsed lease was not taken over")
	}
}

// cuttingRegistry proxies registry and cuts every layer download from the
// platform repositories halfway, as a connection reset does.
func cuttingRegistry(t *testing.T, registry string) string {
	t.Helper()
	upstream := &url.URL{Scheme: "http", Host: registry}
	proxy := httputil.NewSingleHostReverseProxy(upstream)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || !strings.Contains(r.URL.Path, "/lazycloud/platform/") || !strings.Contains(r.URL.Path, "/blobs/") {
			proxy.ServeHTTP(w, r)
			return
		}
		resp, err := http.Get(upstream.String() + r.URL.Path) //nolint:noctx // The test's proxy.
		if err != nil {
			t.Error(err)
			return
		}
		defer func() { _ = resp.Body.Close() }()
		body, _ := io.ReadAll(resp.Body)
		if len(body) < 1024 {
			w.WriteHeader(resp.StatusCode)
			_, _ = w.Write(body)
			return
		}
		w.Header().Set("Content-Length", strconv.Itoa(len(body)))
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body[:len(body)/2])
		panic(http.ErrAbortHandler)
	}))
	t.Cleanup(server.Close)
	return strings.TrimPrefix(server.URL, "http://")
}

// A layer download cut short is the registry's failure, retried later; a
// layer whose blob holds its digest but is a truncated tar is the image's.
// Neither leaves its download behind.
func TestCutDownloadsAreTransientAndTruncatedLayersAreContent(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	leftovers := func() int {
		dirs, err := filepath.Glob(filepath.Join(os.TempDir(), "lazycloud-platform-*"))
		if err != nil {
			t.Fatal(err)
		}
		return len(dirs)
	}
	before := leftovers()

	registry := cuttingRegistry(t, f.registry)
	cut := images.NewImages(f.pool, f.execution, f.secrets, f.storage, images.Config{Registry: registry, Repository: "lazycloud", Insecure: true})
	img, err := random.Image(64<<10, 1)
	if err != nil {
		t.Fatal(err)
	}
	reference := registry + "/tools/cut:1@" + pushImage(t, f.registry+"/tools/cut:1", img)
	err = cut.ConvertPlatformImage(ctx, reference, "amd64")
	var unconvertible *images.ConversionError
	if err == nil || errors.As(err, &unconvertible) || !errors.Is(err, images.ErrRegistryUnavailable) {
		t.Fatalf("a cut download is the registry's failure: %v", err)
	}
	if n := f.count(t, "select count(*) from platform_images where reference = $1 and failure_transient", reference); n != 1 {
		t.Fatal("the cut download is not recorded as transient")
	}

	var layer bytes.Buffer
	tw := tar.NewWriter(&layer)
	if err := tw.WriteHeader(&tar.Header{Name: "a", Typeflag: tar.TypeReg, Mode: 0o644, Size: 1000}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(make([]byte, 100)); err != nil {
		t.Fatal(err)
	}
	truncated, err := mutate.AppendLayers(empty.Image, static.NewLayer(layer.Bytes(), types.DockerUncompressedLayer))
	if err != nil {
		t.Fatal(err)
	}
	broken := f.registry + "/tools/truncated:1@" + pushImage(t, f.registry+"/tools/truncated:1", truncated)
	if err := f.images.ConvertPlatformImage(ctx, broken, "amd64"); !errors.As(err, &unconvertible) {
		t.Fatalf("a truncated layer fails the image: %v", err)
	}
	if n := leftovers(); n != before {
		t.Fatalf("%d conversion directories stayed", n-before)
	}
}
