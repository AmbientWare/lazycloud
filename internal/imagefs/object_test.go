package imagefs

import (
	"archive/tar"
	"bytes"
	"context"
	"crypto/rand"
	"errors"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
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
	var layer bytes.Buffer
	tw := tar.NewWriter(&layer)
	if err := tw.WriteHeader(&tar.Header{Name: "weights", Typeflag: tar.TypeReg, Mode: 0o644, Size: int64(len(body))}); err != nil {
		t.Fatal(err)
	}
	if _, err := tw.Write(body); err != nil {
		t.Fatal(err)
	}
	if err := tw.Close(); err != nil {
		t.Fatal(err)
	}
	var data bytes.Buffer
	ix, err := Convert(ctx, &layer, &data)
	if err != nil {
		t.Fatal(err)
	}

	cfg := storagetest.Config()
	testBucket := cfg.Bucket
	store := s3.New(s3.Options{
		Region: cfg.Region, BaseEndpoint: aws.String(cfg.Endpoint), UsePathStyle: true,
		Credentials: credentials.NewStaticCredentialsProvider(cfg.AccessKeyID, cfg.SecretAccessKey, ""),
	})
	key := "imagefs-test/" + uuid.NewString() + "/data"
	if _, err := store.PutObject(ctx, &s3.PutObjectInput{
		Bucket: aws.String(testBucket), Key: aws.String(key), Body: bytes.NewReader(data.Bytes()),
	}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = store.DeleteObject(context.Background(), &s3.DeleteObjectInput{Bucket: aws.String(testBucket), Key: aws.String(key)})
	})
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
	if _, err := signed.ReadRange(ctx, 0, 1); err == nil || strings.Contains(err.Error(), "secret") {
		t.Fatalf("a failed request reports %v", err)
	}
}
