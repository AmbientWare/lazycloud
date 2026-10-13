package snapshotter

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// upstream counts what the endpoint asks of the store, and runs before on
// each request first.
type upstream struct {
	mu      sync.Mutex
	gets    int
	heads   int
	refused int
	before  func(*http.Request)
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
	defer u.mu.Unlock()
	if r.Method == http.MethodGet && !r.URL.Query().Has("list-type") {
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

// testKey is the key the test's requests to the endpoint sign with.
const testKey = "test-key-0123456789abcdef"

// volumeEndpoint serves the test store's bucket through volumes over a
// cache of 32 MiB, its keys under a volume's prefix.
func volumeEndpoint(t *testing.T, ts *testStore, up *upstream) (*volumes, string) {
	t.Helper()
	ts.prefix = "volumes/" + uuid.NewString() + "/"
	c := newTestCache(t, up, 32<<20)
	v := newVolumes(c, &http.Client{Transport: up}, slog.New(slog.DiscardHandler))
	v.addr = "test"
	cfg := storagetest.Config(t)
	if err := v.grant(uuid.NewString(), testKey, &imagefsproto.BucketGrant{
		Endpoint: cfg.Endpoint, Region: cfg.Region, Bucket: ts.bucket, ForcePathStyle: true,
		AccessKeyId: cfg.AccessKeyID, SecretAccessKey: cfg.SecretAccessKey, ExpiresAt: timestamppb.New(time.Now().Add(time.Hour)),
	}); err != nil {
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

// send sends a request signed with key, as GeeseFS signs.
func send(t *testing.T, method, url, byteRange, key string) (answer, []byte) {
	t.Helper()
	req, err := http.NewRequestWithContext(t.Context(), method, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "AWS4-HMAC-SHA256 Credential="+key+"/20261013/us-east-1/s3/aws4_request, SignedHeaders=host, Signature=0")
	if byteRange != "" {
		req.Header.Set("Range", "bytes="+byteRange)
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
	ts := newTestStore(t)
	up := &upstream{}
	// Objects over a quarter of the cache are read by frame, never whole.
	_, base := volumeEndpoint(t, ts, up)
	old, changed := randomBytes(1, 3*volumeFrame), randomBytes(2, 3*volumeFrame)
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
	if resp, _ := get(t, base+key, fmt.Sprintf("%d-%d", volumeFrame, volumeFrame+99)); resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("a read of a frame the store refused is %s", resp.Status)
	}
	if _, refused := up.counts(); refused == 0 {
		t.Fatal("the store answered no 412 to a frame of the old version")
	}
	resp, body = get(t, base+key, fmt.Sprintf("%d-%d", volumeFrame, volumeFrame+99))
	if resp.StatusCode != http.StatusPartialContent || !bytes.Equal(body, changed[volumeFrame:volumeFrame+100]) {
		t.Fatalf("the read after the change: %s, new bytes %v", resp.Status, bytes.Equal(body, changed[volumeFrame:volumeFrame+100]))
	}
}

// Warming fills a volume's objects, each within a quarter of the cache,
// smallest first within half, so later reads of them reach no store.
func TestWarmedVolumesReadWithoutTheStore(t *testing.T) {
	ts := newTestStore(t)
	up := &upstream{}
	v, base := volumeEndpoint(t, ts, up)
	// Smallest first, b and c fill half the cache, and a, listed first,
	// does not.
	objects := map[string][]byte{"a": randomBytes(3, 7<<20), "b": randomBytes(4, 5<<20), "c": randomBytes(5, 6<<20), "small": randomBytes(6, 1000)}
	for name, body := range objects {
		ts.put(t, "model/"+name, body)
	}
	g := v.buckets[ts.bucket].grant
	v.warm(t.Context(), g, []string{ts.prefix + "model/"})
	gets, _ := up.counts()
	for _, name := range []string{"b", "c"} {
		if resp, body := get(t, base+"model/"+name, ""); resp.StatusCode != http.StatusOK || !bytes.Equal(body, objects[name]) {
			t.Fatalf("read of %s: %s", name, resp.Status)
		}
	}
	if after, _ := up.counts(); after != gets {
		t.Fatalf("reads of warmed objects made %d store GETs", after-gets)
	}
	if resp, body := get(t, base+"model/a", ""); resp.StatusCode != http.StatusOK || !bytes.Equal(body, objects["a"]) {
		t.Fatalf("read of a: %s", resp.Status)
	}
	if after, _ := up.counts(); after == gets {
		t.Fatal("warming filled past half the cache")
	}
}

// Writes reach the store intact through the endpoint, as GeeseFS makes
// them: small files, a multipart upload, a copy in parts as a rename of a
// large file makes, and a delete; and a read right after a write returns
// what was written.
func TestVolumeWritesPassThrough(t *testing.T) {
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
	ts := newTestStore(t)
	_, base := volumeEndpoint(t, ts, &upstream{})
	body := randomBytes(10, 2<<20)
	name := "dir with space/a+b/ünïcode.bin"
	if _, err := endpointClient(base, ts).PutObject(t.Context(), &s3.PutObjectInput{
		Bucket: aws.String(ts.bucket), Key: aws.String(ts.prefix + name), Body: bytes.NewReader(body),
	}); err != nil {
		t.Fatal(err)
	}
	escaped := url.PathEscape("dir with space") + "/" + url.PathEscape("a+b") + "/" + url.PathEscape("ünïcode.bin")
	if resp, got := get(t, base+escaped, ""); resp.StatusCode != http.StatusOK || !bytes.Equal(got, body) {
		t.Fatalf("read of %q: %s", name, resp.Status)
	}
}

// Writes of other keys leave a cached object's version standing, so its
// reads ask the store nothing.
func TestCachedReadsHitUnderSteadyWrites(t *testing.T) {
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
	for range 10 {
		if resp, got := get(t, base+"weights", "0-1048575"); resp.StatusCode != http.StatusPartialContent || !bytes.Equal(got, body[:1<<20]) {
			t.Errorf("read under writes: %s", resp.Status)
		}
	}
	close(stop)
	writes.Wait()
	up.mu.Lock()
	defer up.mu.Unlock()
	if up.heads != heads || up.gets != gets {
		t.Fatalf("reads under writes of other keys asked the store %d HEADs and %d GETs", up.heads-heads, up.gets-gets)
	}
}

// The endpoint answers only requests signed with the bucket's key, and
// only for keys under volumes/: a workspace's grant reaches its disks too.
func TestTheVolumeEndpointRefusesOtherKeysAndPrefixes(t *testing.T) {
	ts := newTestStore(t)
	_, base := volumeEndpoint(t, ts, &upstream{})
	ts.put(t, "file", []byte("volume"))
	bucket := base[:len(base)-len(ts.prefix)]
	for _, c := range []struct {
		method, url, key string
		code             int
	}{
		{http.MethodGet, base + "file", testKey, http.StatusOK},
		{http.MethodGet, base + "file", "another-key-0123456789", http.StatusForbidden},
		{http.MethodGet, base + "file", "", http.StatusForbidden},
		{http.MethodGet, bucket + "disks/" + uuid.NewString() + "/index", testKey, http.StatusForbidden},
		{http.MethodPut, bucket + "disks/x", testKey, http.StatusForbidden},
		{http.MethodGet, bucket + "?list-type=2&prefix=disks/", testKey, http.StatusForbidden},
		{http.MethodGet, bucket + "?list-type=2&prefix=" + ts.prefix, testKey, http.StatusOK},
	} {
		if resp, _ := send(t, c.method, c.url, "", c.key); resp.StatusCode != c.code {
			t.Errorf("%s %s with key %q: %s, want %d", c.method, c.url, c.key, resp.Status, c.code)
		}
	}
}
