package imagefs

import (
	"bytes"
	"crypto/rand"
	"errors"
	"io/fs"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// Frames read back from Garage through presigned range URLs, and a URL
// swapped in after the first one expired serves the next read.
func TestHTTPObjectReadsFramesThroughPresignedURLs(t *testing.T) {
	ctx := t.Context()
	body := make([]byte, 2*FrameSize+FrameSize/2)
	_, _ = rand.Read(body)
	var data bytes.Buffer
	ix, err := Convert(ctx, bytes.NewReader(fileTar(t, "weights", body)), &data)
	if err != nil {
		t.Fatal(err)
	}

	testBucket := storagetest.Config(t).Bucket
	store := storagetest.Client()
	key := "imagefs-test/" + uuid.NewString() + "/data"
	if _, err := store.PutObject(ctx, &s3.PutObjectInput{
		Bucket: aws.String(testBucket), Key: aws.String(key), Body: bytes.NewReader(data.Bytes()),
	}); err != nil {
		t.Fatal(err)
	}
	presign := func(lifetime time.Duration) string {
		req, err := s3.NewPresignClient(store).PresignGetObject(ctx, &s3.GetObjectInput{
			Bucket: aws.String(testBucket), Key: aws.String(key),
		}, s3.WithPresignExpires(lifetime))
		if err != nil {
			t.Fatal(err)
		}
		return req.URL
	}

	current := presign(2 * time.Second)
	object := HTTPObject(http.DefaultClient, func() string { return current })
	frames := map[int][]byte{}
	if frames[0], err = ix.ReadFrame(ctx, object, 0); err != nil {
		t.Fatal(err)
	}
	var refused *StatusError
	for deadline := time.Now().Add(10 * time.Second); ; time.Sleep(250 * time.Millisecond) {
		_, err = ix.ReadFrame(ctx, object, 1)
		if errors.As(err, &refused) || time.Now().After(deadline) {
			break
		}
	}
	// S3 refuses an expired URL with 403, Garage with 400.
	if refused == nil || refused.StatusCode != http.StatusForbidden && refused.StatusCode != http.StatusBadRequest {
		t.Fatalf("a read through the expired URL: %v", err)
	}
	current = presign(time.Hour)
	if got := contents(t, ix, object, frames, ix.Entries[0]); !bytes.Equal(got, body) {
		t.Fatalf("read back %d bytes that differ from the %d written", len(got), len(body))
	}

	signed := HTTPObject(http.DefaultClient, func() string { return "http://127.0.0.1:1/data?X-Amz-Signature=secret" })
	if err := signed.read(ctx, 0, make([]byte, 1)); err == nil || strings.Contains(err.Error(), "secret") {
		t.Fatalf("a failed request reports %v", err)
	}
}

// refusingStore answers every request as S3 answers a bad signature: 403
// with an error body that echoes the request, signature included.
func refusingStore(t *testing.T) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`<?xml version="1.0" encoding="UTF-8"?><Error><Code>SignatureDoesNotMatch</Code>` +
			`<CanonicalRequest>GET ` + r.URL.String() + `</CanonicalRequest></Error>`))
	}))
	t.Cleanup(server.Close)
	return server
}

// A refused read keeps the store's error code and drops the rest of its
// answer, which echoes the URL's signature.
func TestRefusedReadsKeepOnlyTheStoreErrorCode(t *testing.T) {
	server := refusingStore(t)
	url := server.URL + "/layer?X-Amz-Signature=secret"
	ix, _ := roundTrip(t, fileTar(t, "app", []byte("hi")))
	_, frameErr := ix.ReadFrame(t.Context(), HTTPObject(server.Client(), func() string { return url }), 0)
	_, _, indexErr := FetchIndex(t.Context(), server.Client(), url)
	for name, err := range map[string]error{"frame": frameErr, "index": indexErr} {
		var refused *StatusError
		if !errors.As(err, &refused) || refused.Code != "SignatureDoesNotMatch" || strings.Contains(err.Error(), "secret") {
			t.Errorf("a refused %s read: %v", name, err)
		}
	}
}

// A stored index may be larger than the decoded bound it is held to,
// since zstd stores incompressible input with a few bytes of framing.
func TestFetchIndexReadsAnIndexAtTheDecodedBound(t *testing.T) {
	ix := Index{Layer: Digest("sha256:" + strings.Repeat("ab", 32))}
	xattr := make([]byte, MaxIndexSize-200)
	_, _ = rand.Read(xattr)
	ix.Entries = []Entry{{Path: ".", Type: TypeDirectory, Mode: fs.ModeDir | 0o755, ModTime: time.Unix(0, 0).UTC(), Xattrs: map[string][]byte{"user.x": xattr}}}
	stored, err := ix.Marshal()
	if err != nil {
		t.Fatal(err)
	}
	if len(stored) <= MaxIndexSize {
		t.Fatalf("the stored index of %d bytes is within the decoded bound", len(stored))
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(stored) }))
	t.Cleanup(server.Close)
	if _, got, err := FetchIndex(t.Context(), server.Client(), server.URL); err != nil || len(got.Entries) != 1 {
		t.Fatalf("an index decoding within the bound: %v", err)
	}
}
