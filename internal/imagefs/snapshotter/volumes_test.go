package snapshotter

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"maps"
	"math/rand/v2"
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// upstream counts what the endpoint asks of the store, and runs before on
// each request first and after once the store answered it.
type upstream struct {
	mu      sync.Mutex
	gets    int
	heads   int
	refused int
	before  func(*http.Request)
	after   func(*http.Request)
}

func (u *upstream) RoundTrip(r *http.Request) (*http.Response, error) {
	u.mu.Lock()
	before := u.before
	u.mu.Unlock()
	if before != nil {
		before(r)
	}
	resp, err := http.DefaultTransport.RoundTrip(r)
	u.mu.Lock()
	after := u.after
	u.mu.Unlock()
	if after != nil {
		after(r)
	}
	u.mu.Lock()
	defer u.mu.Unlock()
	if r.Method == http.MethodGet && !r.URL.Query().Has("list-type") && r.URL.Path != "/frame" {
		u.gets++
	}
	if r.Method == http.MethodHead {
		u.heads++
	}
	if err == nil && resp.StatusCode == http.StatusPreconditionFailed {
		u.refused++
	}
	return resp, err //nolint:wrapcheck // The transport's own error.
}

func (u *upstream) counts() (int, int) {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.gets, u.refused
}

// testKey is the key the test's requests to the endpoint sign with, as
// the mount of testContainer.
const (
	testKey       = "test-key-0123456789abcdef"
	testContainer = "6b8f3c64-0d1e-4b8e-9a51-2a3c9f0e7d11"
)

// testGrant reaches bucket of the test store for an hour.
func testGrant(t *testing.T, bucket string) *imagefsproto.BucketGrant {
	t.Helper()
	cfg := storagetest.Config(t)
	return &imagefsproto.BucketGrant{
		Endpoint: cfg.Endpoint, Region: cfg.Region, Bucket: bucket, ForcePathStyle: true,
		AccessKeyId: cfg.AccessKeyID, SecretAccessKey: cfg.SecretAccessKey, ExpiresAt: timestamppb.New(time.Now().Add(time.Hour)),
	}
}

// volumeEndpoint serves the test store's bucket through volumes over a
// cache of 32 MiB, its keys under a volume's prefix.
func volumeEndpoint(t *testing.T, ts *testStore, up *upstream) (*volumes, string) {
	t.Helper()
	return serveVolumes(t, ts, up, newTestCache(t, up, 32<<20))
}

// serveVolumes serves the test store's bucket through volumes over c, its
// keys under a volume's prefix, to testContainer's mount.
func serveVolumes(t *testing.T, ts *testStore, up *upstream, c *frameCache) (*volumes, string) {
	t.Helper()
	ts.prefix = "volumes/" + uuid.NewString() + "/"
	v := newVolumes(c, &http.Client{Transport: up}, slog.New(slog.DiscardHandler))
	if err := v.grant(uuid.NewString(), testGrant(t, ts.bucket), testContainer, testKey, nil); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(v)
	t.Cleanup(srv.Close)
	return v, srv.URL + "/" + ts.bucket + "/" + ts.prefix
}

func randomBytes(seed byte, n int) []byte {
	p := make([]byte, n)
	_, _ = rand.NewChaCha8([32]byte{seed}).Read(p)
	return p
}

// get reads url with a Range header, empty for none.
func get(t *testing.T, url, byteRange string) (answer, []byte) {
	t.Helper()
	return send(t, http.MethodGet, url, byteRange, testKey)
}

// answer is a response read whole.
type answer struct {
	StatusCode int
	Status     string
	Header     http.Header
}

// send sends a request signed with key, as GeeseFS signs, with header's
// pairs and body.
func send(t *testing.T, method, url, byteRange, key string, header ...string) (answer, []byte) {
	t.Helper()
	var payload io.Reader
	if len(header)%2 == 1 {
		payload, header = strings.NewReader(header[len(header)-1]), header[:len(header)-1]
	}
	req, err := http.NewRequestWithContext(t.Context(), method, url, payload)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "AWS4-HMAC-SHA256 Credential="+key+"/20261013/us-east-1/s3/aws4_request, SignedHeaders=host, Signature=0")
	if byteRange != "" {
		req.Header.Set("Range", "bytes="+byteRange)
	}
	for i := 0; i < len(header); i += 2 {
		req.Header.Set(header[i], header[i+1])
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
	return answer{StatusCode: resp.StatusCode, Status: resp.Status, Header: resp.Header}, body
}

// A read of a large object is served from one version of it: the object's
// headers come back as the store gave them, a second read reaches no store,
// and a frame fetched after the object changed is refused by the store's
// If-Match, so the read fails rather than mix versions, and the next read
// returns the new version.
func TestVolumeReadsServeOneVersion(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	up := &upstream{}
	// The object fits the cache and is over a quarter of it, so it is read
	// by frame, never whole, and its last frame lies past the read-ahead of
	// a read of its first.
	c := newTestCache(t, up, 64<<20)
	_, base := serveVolumes(t, ts, up, c)
	frames := readAheadFrames + 3
	old, changed := randomBytes(1, frames*volumeFrame), randomBytes(2, frames*volumeFrame)
	key := "weights.bin"
	put := func(body []byte) {
		if _, err := ts.client.PutObject(context.Background(), &s3.PutObjectInput{
			Bucket: aws.String(ts.bucket), Key: aws.String(ts.prefix + key), Body: bytes.NewReader(body),
			ContentType: aws.String("application/x-weights"), Metadata: map[string]string{"symlink": "target"},
		}); err != nil {
			t.Error(err)
		}
	}
	put(old)

	resp, body := get(t, base+key, "0-1048575")
	if resp.StatusCode != http.StatusPartialContent || !bytes.Equal(body, old[:1<<20]) {
		t.Fatalf("first read: %s, %d bytes", resp.Status, len(body))
	}
	if resp.Header.Get("Content-Type") != "application/x-weights" || resp.Header.Get("X-Amz-Meta-Symlink") != "target" ||
		resp.Header.Get("Etag") == "" || resp.Header.Get("Last-Modified") == "" ||
		resp.Header.Get("Content-Range") != fmt.Sprintf("bytes 0-1048575/%d", len(old)) {
		t.Fatalf("the cached read answers headers %v", resp.Header)
	}
	c.background.Wait()
	gets, _ := up.counts()
	if resp, body = get(t, base+key, "0-1048575"); resp.StatusCode != http.StatusPartialContent || !bytes.Equal(body, old[:1<<20]) {
		t.Fatalf("second read: %s", resp.Status)
	}
	if again, _ := up.counts(); again != gets {
		t.Fatalf("a second read of a cached frame made %d store GETs", again-gets)
	}

	var once sync.Once
	up.mu.Lock()
	up.before = func(r *http.Request) {
		if r.Header.Get("If-Match") != "" {
			once.Do(func() { put(changed) })
		}
	}
	up.mu.Unlock()
	last := int64(frames-1) * volumeFrame
	if resp, _ := get(t, base+key, fmt.Sprintf("%d-%d", last, last+99)); resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("a read of a frame the store refused is %s", resp.Status)
	}
	if _, refused := up.counts(); refused == 0 {
		t.Fatal("the store answered no 412 to a frame of the old version")
	}
	resp, body = get(t, base+key, fmt.Sprintf("%d-%d", last, last+99))
	if resp.StatusCode != http.StatusPartialContent || !bytes.Equal(body, changed[last:last+100]) {
		t.Fatalf("the read after the change: %s, new bytes %v", resp.Status, bytes.Equal(body, changed[last:last+100]))
	}
}

// A read whose HEAD the store answered before an overwrite, but which
// reached the endpoint only after a listing named the new version, answers
// the new version: the client that listed it keeps what the read returns
// as that version's bytes.
func TestAReadAfterAListingAnswersTheListedVersion(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	up := &upstream{}
	_, base := volumeEndpoint(t, ts, up)
	old, changed := randomBytes(5, 2<<20), randomBytes(6, 2<<20)
	ts.put(t, "model.bin", old)
	held, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	up.mu.Lock()
	up.after = func(r *http.Request) {
		if r.Method == http.MethodHead {
			once.Do(func() {
				close(held)
				<-release
			})
		}
	}
	up.mu.Unlock()
	type read struct {
		status int
		body   []byte
		err    error
	}
	reads := make(chan read, 1)
	go func() {
		req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, base+"model.bin", nil)
		if err != nil {
			reads <- read{err: err}
			return
		}
		req.Header.Set("Authorization", "AWS4-HMAC-SHA256 Credential="+testKey+"/20261013/us-east-1/s3/aws4_request, SignedHeaders=host, Signature=0")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			reads <- read{err: err}
			return
		}
		defer func() { _ = resp.Body.Close() }()
		body, err := io.ReadAll(resp.Body)
		reads <- read{status: resp.StatusCode, body: body, err: err}
	}()
	<-held
	ts.put(t, "model.bin", changed)
	bucket := strings.TrimSuffix(base, ts.prefix)
	if resp, body := get(t, bucket+"?list-type=2&prefix="+url.QueryEscape(ts.prefix), ""); resp.StatusCode != http.StatusOK || !bytes.Contains(body, []byte("model.bin")) {
		t.Fatalf("listing: %s %s", resp.Status, body)
	}
	close(release)
	r := <-reads
	if r.err != nil || r.status != http.StatusOK || !bytes.Equal(r.body, changed) {
		t.Fatalf("the read after the listing answered %d, the new bytes %v: %v", r.status, bytes.Equal(r.body, changed), r.err)
	}
}

// Writes reach the store intact through the endpoint, as GeeseFS makes
// them: small files, a multipart upload, a copy in parts as a rename of a
// large file makes, and a delete; and a read right after a write returns
// what was written.
func TestVolumeWritesPassThrough(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	_, base := volumeEndpoint(t, ts, &upstream{})
	client := endpointClient(base, ts)
	ctx := t.Context()
	bucket := aws.String(ts.bucket)
	key := func(name string) *string { return aws.String(ts.prefix + name) }
	// GeeseFS's GETs carry no query string, as these do; the SDK's add one.
	read := func(name string) []byte {
		t.Helper()
		resp, body := get(t, base+name, "")
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("read %s: %s", name, resp.Status)
		}
		return body
	}

	for n := range 64 {
		name := fmt.Sprintf("small/%02d", n)
		if _, err := client.PutObject(ctx, &s3.PutObjectInput{Bucket: bucket, Key: key(name), Body: bytes.NewReader([]byte(name))}); err != nil {
			t.Fatal(err)
		}
	}
	for n := range 64 {
		if name := fmt.Sprintf("small/%02d", n); string(read(name)) != name {
			t.Fatalf("%s reads back otherwise", name)
		}
	}

	first, second := randomBytes(7, 5<<20), randomBytes(8, 3<<20)
	whole := append(append([]byte{}, first...), second...)
	upload, err := client.CreateMultipartUpload(ctx, &s3.CreateMultipartUploadInput{Bucket: bucket, Key: key("big")})
	if err != nil {
		t.Fatal(err)
	}
	var parts []types.CompletedPart
	for n, body := range [][]byte{first, second} {
		part, err := client.UploadPart(ctx, &s3.UploadPartInput{
			Bucket: bucket, Key: key("big"), UploadId: upload.UploadId, PartNumber: aws.Int32(int32(n + 1)), Body: bytes.NewReader(body),
		})
		if err != nil {
			t.Fatal(err)
		}
		parts = append(parts, types.CompletedPart{ETag: part.ETag, PartNumber: aws.Int32(int32(n + 1))})
	}
	if _, err := client.CompleteMultipartUpload(ctx, &s3.CompleteMultipartUploadInput{
		Bucket: bucket, Key: key("big"), UploadId: upload.UploadId, MultipartUpload: &types.CompletedMultipartUpload{Parts: parts},
	}); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(read("big"), whole) {
		t.Fatal("the multipart upload reads back otherwise")
	}

	copied, err := client.CreateMultipartUpload(ctx, &s3.CreateMultipartUploadInput{Bucket: bucket, Key: key("renamed")})
	if err != nil {
		t.Fatal(err)
	}
	parts = nil
	for n, r := range []string{"bytes=0-5242879", fmt.Sprintf("bytes=5242880-%d", len(whole)-1)} {
		part, err := client.UploadPartCopy(ctx, &s3.UploadPartCopyInput{
			Bucket: bucket, Key: key("renamed"), UploadId: copied.UploadId, PartNumber: aws.Int32(int32(n + 1)),
			CopySource: aws.String(ts.bucket + "/" + ts.prefix + "big"), CopySourceRange: aws.String(r),
		})
		if err != nil {
			t.Fatal(err)
		}
		parts = append(parts, types.CompletedPart{ETag: part.CopyPartResult.ETag, PartNumber: aws.Int32(int32(n + 1))})
	}
	if _, err := client.CompleteMultipartUpload(ctx, &s3.CompleteMultipartUploadInput{
		Bucket: bucket, Key: key("renamed"), UploadId: copied.UploadId, MultipartUpload: &types.CompletedMultipartUpload{Parts: parts},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := client.DeleteObject(ctx, &s3.DeleteObjectInput{Bucket: bucket, Key: key("big")}); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(read("renamed"), whole) {
		t.Fatal("the copy reads back otherwise")
	}
	if _, err := client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: bucket, Key: key("big")}); err == nil {
		t.Fatal("the deleted object is still there")
	}

	rewritten := randomBytes(9, 2<<20)
	if _, err := client.PutObject(ctx, &s3.PutObjectInput{Bucket: bucket, Key: key("renamed"), Body: bytes.NewReader(rewritten)}); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(read("renamed"), rewritten) {
		t.Fatal("a read right after a write returns the old bytes")
	}
}

// endpointClient signs requests to the endpoint base serves with testKey.
func endpointClient(base string, ts *testStore) *s3.Client {
	return s3.New(s3.Options{
		Region: "us-east-1", BaseEndpoint: aws.String(base[:len(base)-len("/"+ts.bucket+"/"+ts.prefix)]), UsePathStyle: true,
		Credentials:                credentials.NewStaticCredentialsProvider(testKey, testKey, ""),
		RequestChecksumCalculation: aws.RequestChecksumCalculationWhenRequired,
		ResponseChecksumValidation: aws.ResponseChecksumValidationWhenRequired,
	})
}

// Keys with spaces, plus signs and non-ASCII letters read through the
// cache and write through the endpoint like any other.
func TestVolumeKeysKeepTheirNames(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	_, base := volumeEndpoint(t, ts, &upstream{})
	body := randomBytes(10, 2<<20)
	name := "dir with space/a+b/ünïcode.bin"
	if _, err := endpointClient(base, ts).PutObject(t.Context(), &s3.PutObjectInput{
		Bucket: aws.String(ts.bucket), Key: aws.String(ts.prefix + name), Body: bytes.NewReader(body),
	}); err != nil {
		t.Fatal(err)
	}
	// GeeseFS escapes keys as SigV4 does: every byte but unreserved ones
	// and "/".
	var escaped strings.Builder
	for _, c := range []byte(name) {
		if 'A' <= c && c <= 'Z' || 'a' <= c && c <= 'z' || '0' <= c && c <= '9' || strings.IndexByte("-_.~/", c) >= 0 {
			escaped.WriteByte(c)
		} else {
			fmt.Fprintf(&escaped, "%%%02X", c)
		}
	}
	if resp, got := get(t, base+escaped.String(), ""); resp.StatusCode != http.StatusOK || !bytes.Equal(got, body) {
		t.Fatalf("read of %q: %s", name, resp.Status)
	}
}

// Writes of other keys leave a cached object's version standing, so its
// reads ask the store nothing.
func TestCachedReadsHitUnderSteadyWrites(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	up := &upstream{}
	_, base := volumeEndpoint(t, ts, up)
	body := randomBytes(11, 2<<20)
	ts.put(t, "weights", body)
	if resp, _ := get(t, base+"weights", "0-1048575"); resp.StatusCode != http.StatusPartialContent {
		t.Fatalf("first read: %s", resp.Status)
	}
	up.mu.Lock()
	heads, gets := up.heads, up.gets
	up.mu.Unlock()
	stop := make(chan struct{})
	var writes sync.WaitGroup
	writes.Go(func() {
		for n := 0; ; n++ {
			select {
			case <-stop:
				return
			default:
			}
			if resp, _ := send(t, http.MethodDelete, base+fmt.Sprintf("other-%d", n), "", testKey); resp.StatusCode >= 300 {
				t.Errorf("delete: %s", resp.Status)
				return
			}
		}
	})
	const reads = 20
	began := time.Now()
	for range reads {
		if resp, got := get(t, base+"weights", "0-1048575"); resp.StatusCode != http.StatusPartialContent || !bytes.Equal(got, body[:1<<20]) {
			t.Errorf("read under writes: %s", resp.Status)
		}
	}
	elapsed := time.Since(began)
	close(stop)
	writes.Wait()
	up.mu.Lock()
	defer up.mu.Unlock()
	// The version is asked again once a second, however slow the reads.
	if asked := up.heads - heads; up.gets != gets || asked > int(elapsed/observationLife)+1 {
		t.Fatalf("%d reads in %v under writes of other keys asked the store %d HEADs and %d GETs", reads, elapsed, asked, up.gets-gets)
	}
}

// The endpoint answers only requests signed with the bucket's key, so one
// workspace's mounts cannot reach another's bucket, and only for keys
// under volumes/: a workspace's grant reaches its disks too.
func TestTheVolumeEndpointRefusesOtherKeysAndPrefixes(t *testing.T) {
	t.Parallel()
	ts := newTestStore(t)
	v, base := volumeEndpoint(t, ts, &upstream{})
	ts.put(t, "file", []byte("volume"))
	bucket := base[:len(base)-len(ts.prefix)]
	if err := v.grant(uuid.NewString(), testGrant(t, "other-workspace-bucket"), uuid.NewString(), "other-workspace-key-0123", nil); err != nil {
		t.Fatal(err)
	}
	other := bucket[:len(bucket)-len(ts.bucket)-1] + "other-workspace-bucket/"
	deleteDisk := `<Delete><Object><Key>` + ts.prefix + `file</Key></Object><Object><Key>disks/x/index</Key></Object></Delete>`
	for _, c := range []struct {
		method, url, key string
		code             int
		header           []string
	}{
		{http.MethodGet, base + "file", testKey, http.StatusOK, nil},
		{http.MethodGet, base + "file", "another-key-0123456789", http.StatusForbidden, nil},
		{http.MethodGet, base + "file", "", http.StatusForbidden, nil},
		{http.MethodGet, bucket + "disks/" + uuid.NewString() + "/index", testKey, http.StatusForbidden, nil},
		{http.MethodPut, bucket + "disks/x", testKey, http.StatusForbidden, nil},
		{http.MethodGet, bucket + "?list-type=2&prefix=disks/", testKey, http.StatusForbidden, nil},
		{http.MethodGet, bucket + "?list-type=2&prefix=" + ts.prefix, testKey, http.StatusOK, nil},
		{http.MethodGet, bucket + "?list-type=2&prefix=" + ts.prefix + "&prefix=disks/", testKey, http.StatusForbidden, nil},
		{http.MethodGet, other + "volumes/x", testKey, http.StatusForbidden, nil},
		{http.MethodPut, base + "copied", testKey, http.StatusForbidden, []string{"X-Amz-Copy-Source", ts.bucket + "/disks/x/index"}},
		{http.MethodPost, bucket + "?delete", testKey, http.StatusForbidden, []string{deleteDisk}},
	} {
		if resp, _ := send(t, c.method, c.url, "", c.key, c.header...); resp.StatusCode != c.code {
			t.Errorf("%s %s with key %q: %s, want %d", c.method, c.url, c.key, resp.Status, c.code)
		}
	}
}

// Each container's mount records the objects of 1 MiB or more it reads,
// each version once in first-read order, apart from the other mounts of its
// workspace's bucket and at most MaxVolumeReads of them. Reads after its
// record ended, and the mount's key once released, count for nothing.
func TestVolumeTracesArePerContainerAndBounded(t *testing.T) {
	ts := newTestStore(t)
	v, base := volumeEndpoint(t, ts, &upstream{})
	sources := volumeSources{volumes: v}
	other, otherKey := uuid.NewString(), "other-container-key-0123"
	if err := v.grant(v.buckets[ts.bucket].grant.workspace, testGrant(t, ts.bucket), other, otherKey, nil); err != nil {
		t.Fatal(err)
	}
	volume := strings.TrimSuffix(strings.TrimPrefix(ts.prefix, volumesPrefix), "/")
	ts.put(t, "a", randomBytes(1, 2<<20))
	ts.put(t, "b", randomBytes(2, 1<<20))
	ts.put(t, "small", randomBytes(3, 100<<10))
	read := func(key, name string) {
		t.Helper()
		if resp, _ := send(t, http.MethodGet, base+name, "0-0", key); resp.StatusCode != http.StatusPartialContent {
			t.Fatalf("read of %s: %s", name, resp.Status)
		}
	}
	for _, name := range []string{"b", "a", "b", "small"} {
		read(testKey, name)
	}
	read(otherKey, "a")
	const extra = hostproto.MaxVolumeReads
	for n := range extra {
		ts.put(t, fmt.Sprintf("many/%03d", n), randomBytes(byte(n), 1<<20))
		read(testKey, fmt.Sprintf("many/%03d", n))
	}
	paths := func(container string) []string {
		t.Helper()
		answer, err := sources.VolumeReads(t.Context(), &imagefsproto.VolumeReadsRequest{ContainerId: container})
		if err != nil {
			t.Fatal(err)
		}
		var out []string
		for _, r := range answer.GetReads() {
			if r.GetVolumeId() != volume {
				t.Fatalf("a read of %s names %v", container, r)
			}
			out = append(out, r.GetPath())
		}
		return out
	}
	if got := paths(testContainer); len(got) != hostproto.MaxVolumeReads || got[0] != "b" || got[1] != "a" || got[2] != "many/000" {
		t.Fatalf("the first mount's trace holds %d reads beginning %v", len(got), got[:min(3, len(got))])
	}
	if got := paths(other); !slices.Equal(got, []string{"a"}) {
		t.Fatalf("the other mount's trace is %v", got)
	}
	read(otherKey, "b")
	if got := paths(other); !slices.Equal(got, []string{"a"}) {
		t.Fatalf("a read after the trace ended joined it: %v", got)
	}
	if _, err := sources.ReleaseVolumes(t.Context(), &imagefsproto.ReleaseVolumesRequest{ContainerId: other}); err != nil {
		t.Fatal(err)
	}
	if resp, _ := send(t, http.MethodGet, base+"a", "0-0", otherKey); resp.StatusCode != http.StatusForbidden {
		t.Fatalf("a released mount's key reads: %s", resp.Status)
	}
}

// volumeRead names the object name of the test store's volume.
func volumeRead(ts *testStore, name string) *imagefsproto.VolumeRead {
	volume := strings.TrimSuffix(strings.TrimPrefix(ts.prefix, volumesPrefix), "/")
	return &imagefsproto.VolumeRead{VolumeId: volume, Path: name}
}

// A warm fills the objects a trace names in its order at their current
// versions, so their reads reach no store, an object overwritten since the
// trace included; it stops where the host's warms would pass half the
// cache, leaving later objects to their readers.
func TestWarmingFillsTheTraceAtCurrentVersions(t *testing.T) {
	ts := newTestStore(t)
	up := &upstream{}
	v, base := volumeEndpoint(t, ts, up)
	objects := map[string][]byte{"a": randomBytes(3, 7<<20), "b": randomBytes(4, 6<<20), "c": randomBytes(5, 5<<20)}
	for name, body := range objects {
		ts.put(t, name, body)
	}
	traced := volumeRead(ts, "b")
	objects["b"] = randomBytes(6, 6<<20)
	ts.put(t, "b", objects["b"])
	if _, err := (volumeSources{volumes: v}).WarmVolumes(t.Context(), &imagefsproto.WarmVolumesRequest{
		WorkspaceId: v.buckets[ts.bucket].grant.workspace, Reads: []*imagefsproto.VolumeRead{traced, volumeRead(ts, "a"), volumeRead(ts, "c")},
	}); err != nil {
		t.Fatal(err)
	}
	v.cache.background.Wait()
	gets, _ := up.counts()
	for _, name := range []string{"b", "a"} {
		if resp, body := get(t, base+name, ""); resp.StatusCode != http.StatusOK || !bytes.Equal(body, objects[name]) {
			t.Fatalf("read of %s: %s, current bytes %v", name, resp.Status, bytes.Equal(body, objects[name]))
		}
	}
	if after, _ := up.counts(); after != gets {
		t.Fatalf("reads of warmed objects made %d store GETs", after-gets)
	}
	if resp, body := get(t, base+"c", ""); resp.StatusCode != http.StatusOK || !bytes.Equal(body, objects["c"]) {
		t.Fatalf("read of c: %s", resp.Status)
	}
	if after, _ := up.counts(); after == gets {
		t.Fatal("warming filled past half the cache")
	}
}

// A read that starts an object fetches the frames after it, so the reads
// that follow it reach no store, a read that jumps elsewhere fetches
// nothing past itself, and the frames one read spans are fetched in
// parallel. The object is larger than a quarter of the cache, so no fill
// fetches it whole.
func TestSequentialReadsFetchAheadIntoMemory(t *testing.T) {
	ts := newTestStore(t)
	var mu sync.Mutex
	fetched := map[int]int{}
	// A fetch of frame 6 waits for one of frame 7 to begin.
	seven := make(chan struct{})
	var parallel atomic.Bool
	up := &upstream{before: func(r *http.Request) {
		var first int64
		if _, err := fmt.Sscanf(r.Header.Get("Range"), "bytes=%d-", &first); err != nil || r.Header.Get("If-Match") == "" {
			return
		}
		frame := int(first / volumeFrame)
		mu.Lock()
		fetched[frame]++
		mu.Unlock()
		switch frame {
		case 7:
			close(seven)
		case 6:
			select {
			case <-seven:
				parallel.Store(true)
			case <-time.After(10 * time.Second):
			}
		}
	}}
	c := newTestCache(t, up, 256<<20)
	_, base := serveVolumes(t, ts, up, c)
	body := randomBytes(15, 12*volumeFrame)
	ts.put(t, "weights", body)
	read := func(from, to int64) {
		t.Helper()
		if resp, got := get(t, base+"weights", fmt.Sprintf("%d-%d", from, to)); resp.StatusCode != http.StatusPartialContent || !bytes.Equal(got, body[from:to+1]) {
			t.Fatalf("read of %d-%d: %s", from, to, resp.Status)
		}
		c.background.Wait()
	}
	frames := func() map[int]int {
		mu.Lock()
		defer mu.Unlock()
		return maps.Clone(fetched)
	}
	ahead := map[int]int{}
	for frame := range readAheadFrames + 1 {
		ahead[frame] = 1
	}
	read(0, 99)
	if got := frames(); !maps.Equal(got, ahead) {
		t.Fatalf("a read starting the object fetched frames %v", got)
	}
	read(10*volumeFrame, 10*volumeFrame+99)
	read(volumeFrame, (readAheadFrames+1)*volumeFrame-1)
	ahead[10] = 1
	if got := frames(); !maps.Equal(got, ahead) {
		t.Fatalf("after a jump and a read of the frames fetched ahead, the store sent frames %v", got)
	}
	read(6*volumeFrame+100, 7*volumeFrame+100)
	if !parallel.Load() {
		t.Fatal("a read across two frames fetched them one after the other")
	}
}
