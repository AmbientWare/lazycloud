package storage_test

import (
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	. "github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

func countObjects(t *testing.T, bucket, prefix string) int {
	t.Helper()
	n := 0
	pages := s3.NewListObjectsV2Paginator(storagetest.Client(), &s3.ListObjectsV2Input{Bucket: aws.String(bucket), Prefix: aws.String(prefix)})
	for pages.HasMorePages() {
		page, err := pages.NextPage(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		n += len(page.Contents)
	}
	return n
}

func putObjects(t *testing.T, bucket, prefix string, n int) {
	t.Helper()
	client := storagetest.Client()
	var wg sync.WaitGroup
	sem := make(chan struct{}, 32)
	for i := range n {
		sem <- struct{}{}
		wg.Go(func() {
			defer func() { <-sem }()
			if _, err := client.PutObject(t.Context(), &s3.PutObjectInput{
				Bucket: aws.String(bucket), Key: aws.String(fmt.Sprintf("%sf%05d", prefix, i)), Body: strings.NewReader("x"),
			}); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
}

// TestSweepDeletesLargePrefixesInChunks: a deleted volume with more files
// than one step removes keeps its row until its prefix is empty.
func TestSweepDeletesLargePrefixesInChunks(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	s := f.storage
	volume, err := s.CreateVolume(ctx, f.ws, "data")
	if err != nil {
		t.Fatal(err)
	}
	putFile(t, s, f, "first", []byte("x"))
	bucket := bucketOf(t, f)
	prefix := "volumes/" + volume.Id.String() + "/"
	putObjects(t, bucket, prefix, 1000)
	if err := s.DeleteVolume(ctx, f.ws, "data"); err != nil {
		t.Fatal(err)
	}
	if _, err := s.Sweep(ctx, slog.Default()); err != nil {
		t.Fatal(err)
	}
	var rows int
	if err := f.pool.QueryRow(ctx, `select count(*) from volumes where id = $1`, volume.Id).Scan(&rows); err != nil || rows != 1 {
		t.Fatalf("row after one chunk: %d err=%v", rows, err)
	}
	if left := countObjects(t, bucket, prefix); left != 1 {
		t.Fatalf("%d objects after one chunk, want 1", left)
	}
	if _, err := s.Sweep(ctx, slog.Default()); err != nil {
		t.Fatal(err)
	}
	if err := f.pool.QueryRow(ctx, `select count(*) from volumes where id = $1`, volume.Id).Scan(&rows); err != nil || rows != 0 {
		t.Fatalf("row after the prefix emptied: %d err=%v", rows, err)
	}
}

// TestSweepRemovesObjectsWithoutOwners covers bytes written after their
// owner's delete, through an upload URL or a host key: objects under ids
// with no row go, owned ones stay.
func TestSweepRemovesObjectsWithoutOwners(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	s := f.storage
	SetOrphanAge(s, 0)
	if _, err := s.CreateVolume(ctx, f.ws, "data"); err != nil {
		t.Fatal(err)
	}
	putFile(t, s, f, "kept.txt", []byte("x"))
	bucket := bucketOf(t, f)
	orphanVolume := "volumes/" + uuid.NewString() + "/"
	orphanDisk := "disks/" + uuid.NewString() + "/"
	putObjects(t, bucket, orphanVolume, 3)
	putObjects(t, bucket, orphanDisk, 2)
	orphanArtifact := fmt.Sprintf("workspaces/%s/artifacts/%s", f.ws, uuid.NewString())
	if _, err := storagetest.Client().PutObject(ctx, &s3.PutObjectInput{
		Bucket: aws.String(f.bucket), Key: aws.String(orphanArtifact), Body: strings.NewReader("x"),
	}); err != nil {
		t.Fatal(err)
	}

	result, err := s.Sweep(ctx, slog.Default())
	if err != nil {
		t.Fatal(err)
	}
	if result.Orphans != 3 {
		t.Fatalf("orphans removed: %d, want two prefixes and one artifact", result.Orphans)
	}
	for _, p := range []string{orphanVolume, orphanDisk} {
		if n := countObjects(t, bucket, p); n != 0 {
			t.Fatalf("%s holds %d objects after the sweep", p, n)
		}
	}
	if n := countObjects(t, f.bucket, orphanArtifact); n != 0 {
		t.Fatalf("orphaned artifact remains")
	}
	listing, err := s.ListVolumeFiles(ctx, f.ws, "data", "", "", 10)
	if err != nil || len(listing.Files) != 1 {
		t.Fatalf("owned volume after the sweep: %v err=%v", paths(listing.Files), err)
	}
}

func TestWriteURLsAreShortLived(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, volumeSpec)
	if _, err := f.storage.CreateVolume(ctx, f.ws, "data"); err != nil {
		t.Fatal(err)
	}
	week := 7 * 24 * 3600
	url, err := f.storage.PresignVolumeFile(ctx, f.ws, "data", apitypes.PresignVolumeFileRequest{
		Path: "x", Method: apitypes.PresignVolumeFileRequestMethodPut, ExpiresSeconds: &week,
	})
	if err != nil || time.Until(url.ExpiresAt) > time.Hour+time.Minute {
		t.Fatalf("put URL expires %v err=%v, want within an hour", url.ExpiresAt, err)
	}
}
