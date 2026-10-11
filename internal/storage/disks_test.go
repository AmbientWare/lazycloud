package storage_test

import (
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	. "github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

const diskSpec = `{"name":"fn","disks":[{"name":"root","size_bytes":2147483648,"mount_path":"/"}]}`

func generation(n int64) DiskGeneration {
	return DiskGeneration{Generation: n, IndexSHA256: fmt.Sprintf("%064d", n)}
}

func indexKey(disk uuid.UUID, n int64) string {
	return fmt.Sprintf("disks/%s/manifests/%012d-%064d", disk, n, n)
}

// TestDiskLeaseFencesHolders covers one writer at a time: a second container
// waits while the first holds the disk, stale holders cannot publish, and a
// lost host frees the disk.
func TestDiskLeaseFencesHolders(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, diskSpec)
	s := f.storage
	first := f.container()

	lease, err := s.AcquireDisk(ctx, f.host, first, "root")
	if err != nil {
		t.Fatal(err)
	}
	if lease.SizeBytes != 2<<30 || lease.Newest != nil || lease.Read.Bucket == "" || lease.Read.AccessKeyID == "" {
		t.Fatalf("first lease: %+v", lease)
	}
	again, err := s.AcquireDisk(ctx, f.host, first, "root")
	if err != nil || string(again.Token) != string(lease.Token) {
		t.Fatalf("reacquire by the holder: err=%v", err)
	}
	if _, err := s.AcquireDisk(ctx, f.host, first, "undeclared"); !errors.Is(err, ErrStaleLease) {
		t.Fatalf("undeclared disk: %v", err)
	}

	if _, err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(1), 100); err != nil {
		t.Fatal(err)
	}
	// A replay of the recorded generation is accepted; a skip is not.
	if _, err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(1), 100); err != nil {
		t.Fatalf("replay: %v", err)
	}
	var out *ConflictError
	if _, err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(3), 100); !errors.As(err, &out) {
		t.Fatalf("skipped generation: %v", err)
	}
	if _, err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, []byte("forged"), generation(2), 100); !errors.Is(err, ErrStaleLease) {
		t.Fatalf("forged token: %v", err)
	}
	if _, err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(2), 100); err != nil {
		t.Fatal(err)
	}

	second := f.container()
	if _, err := s.AcquireDisk(ctx, f.host, second, "root"); !errors.As(err, &out) {
		t.Fatalf("second holder while the first runs: %v", err)
	}
	disk, err := s.GetDisk(ctx, f.ws, "root")
	if err != nil || disk.Status != apitypes.Attached || disk.Generation != 2 || disk.StoredBytes != 200 {
		t.Fatalf("attached disk: %+v err=%v", disk, err)
	}
	if err := s.DeleteDisk(ctx, f.ws, "root"); !errors.As(err, &out) {
		t.Fatalf("delete while attached: %v", err)
	}

	// Stopped without a release, the disk is saving and still held.
	f.stop(first, "stopped")
	if disk, _ := s.GetDisk(ctx, f.ws, "root"); disk.Status != apitypes.Saving {
		t.Fatalf("stopped holder: %s, want saving", disk.Status)
	}
	if _, err := s.AcquireDisk(ctx, f.host, second, "root"); !errors.As(err, &out) {
		t.Fatalf("acquire while saving: %v", err)
	}
	// A stopped holder publishes its final generation, then releases; after
	// that it publishes no more.
	if _, err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(3), 100); err != nil {
		t.Fatalf("final publish after stop: %v", err)
	}
	if err := s.ReleaseDisk(ctx, first, lease.Disk, lease.Token); err != nil {
		t.Fatal(err)
	}
	if _, err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(4), 100); !errors.Is(err, ErrStaleLease) {
		t.Fatalf("publish after release: %v", err)
	}

	next, err := s.AcquireDisk(ctx, f.host, second, "root")
	if err != nil || string(next.Token) == string(lease.Token) || next.Newest == nil || *next.Newest != generation(3) {
		t.Fatalf("lease after release: %+v err=%v", next, err)
	}
	if err := s.ReleaseDisk(ctx, first, lease.Disk, lease.Token); !errors.Is(err, ErrStaleLease) {
		t.Fatalf("release by a replaced holder: %v", err)
	}

	if _, err := s.RecordDiskGeneration(ctx, f.host, second, next.Disk, next.Token, generation(4), 100); err != nil {
		t.Fatal(err)
	}
	if err := s.CollectDisk(ctx, f.host, second, next.Disk, next.Token, 4, nil, 150); err != nil {
		t.Fatal(err)
	}

	// Host loss ends the hold without a release.
	f.stop(second, "host_lost")
	third := f.container()
	last, err := s.AcquireDisk(ctx, f.host, third, "root")
	if err != nil || last.Newest == nil || last.Newest.Generation != 4 {
		t.Fatalf("lease after host loss: %+v err=%v", last, err)
	}
	f.stop(third, "host_lost")
	if err := s.DeleteDisk(ctx, f.ws, "root"); err != nil {
		t.Fatal(err)
	}
	page, err := s.ListDisks(ctx, f.ws, "", 100)
	if err != nil || len(page.Disks) != 0 {
		t.Fatalf("disks after delete: %+v err=%v", page, err)
	}
}

// TestDiskCollectionIsFencedByTheLease covers deleting the objects a disk's
// newest generation no longer reads: only the current holder deletes, only
// indexes below the generation and frames of its own disk, and a failure
// is recorded until a later publish clears it.
func TestDiskCollectionIsFencedByTheLease(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, diskSpec)
	s := f.storage
	first := f.container()
	lease, err := s.AcquireDisk(ctx, f.host, first, "root")
	if err != nil {
		t.Fatal(err)
	}
	for n := int64(1); n <= 2; n++ {
		if _, err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(n), 100); err != nil {
			t.Fatal(err)
		}
	}
	read, err := s.GrantDiskRead(ctx, f.host, first, lease.Disk, lease.Token)
	if err != nil || read.Bucket != lease.Read.Bucket || read.AccessKeyID == "" {
		t.Fatalf("read grant of the holder: %+v err=%v", read, err)
	}
	prefix := "disks/" + lease.Disk.String() + "/"
	sum := fmt.Sprintf("%064x", 9)
	oldManifest := indexKey(lease.Disk, 1)
	chunk := prefix + "frames/" + sum
	other := "disks/" + uuid.NewString() + "/frames/" + sum
	for _, key := range []string{oldManifest, chunk, other} {
		if _, err := storagetest.Client().PutObject(ctx, &s3.PutObjectInput{Bucket: &read.Bucket, Key: aws.String(key), Body: strings.NewReader("x")}); err != nil {
			t.Fatal(err)
		}
	}
	present := func(key string) bool {
		_, err := storagetest.Client().HeadObject(ctx, &s3.HeadObjectInput{Bucket: &read.Bucket, Key: aws.String(key)})
		return err == nil
	}

	var invalid *InvalidError
	for _, keys := range [][]string{{other}, {indexKey(lease.Disk, 2)}, {prefix + "../volumes/x"}} {
		if err := s.CollectDisk(ctx, f.host, first, lease.Disk, lease.Token, 2, keys, 1); !errors.As(err, &invalid) {
			t.Errorf("collecting %v: %v, want InvalidError", keys, err)
		}
	}
	if !present(other) {
		t.Fatal("a refused collection deleted another disk's frame")
	}

	// Once the disk changed hands, the old holder deletes nothing.
	if err := s.RecordDiskFailure(ctx, first, lease.Disk, lease.Token, &DiskFailure{Operation: apitypes.DiskOperationRelease, Message: "upload refused"}); err != nil {
		t.Fatal(err)
	}
	f.stop(first, "host_lost")
	second := f.container()
	next, err := s.AcquireDisk(ctx, f.host, second, "root")
	if err != nil {
		t.Fatal(err)
	}
	if disk, _ := s.GetDisk(ctx, f.ws, "root"); disk.Failure != nil {
		t.Fatalf("a new holder shows the old holder's failure %+v", disk.Failure)
	}
	if err := s.CollectDisk(ctx, f.host, first, lease.Disk, lease.Token, 2, []string{oldManifest, chunk}, 1); !errors.Is(err, ErrStaleLease) {
		t.Fatalf("collection by a replaced holder: %v", err)
	}
	if !present(oldManifest) || !present(chunk) {
		t.Fatal("a replaced holder's collection deleted objects")
	}
	if _, err := s.GrantDiskRead(ctx, f.host, first, lease.Disk, lease.Token); !errors.Is(err, ErrStaleLease) {
		t.Fatalf("read grant of a replaced holder: %v", err)
	}
	if err := s.CollectDisk(ctx, f.host, second, next.Disk, next.Token, 2, []string{oldManifest, chunk}, 1); err != nil {
		t.Fatal(err)
	}
	if present(oldManifest) || present(chunk) || !present(other) {
		t.Fatal("the holder's collection did not delete exactly its keys")
	}
	if disk, _ := s.GetDisk(ctx, f.ws, "root"); disk.StoredBytes != 199 {
		t.Fatalf("stored bytes after collecting 1 byte of 200: %d", disk.StoredBytes)
	}

	// A failure shows on the disk until the next generation.
	if err := s.RecordDiskFailure(ctx, second, next.Disk, next.Token, &DiskFailure{Operation: apitypes.DiskOperationPublish, Message: "chunk upload refused"}); err != nil {
		t.Fatal(err)
	}
	disk, err := s.GetDisk(ctx, f.ws, "root")
	if err != nil || disk.Failure == nil || disk.Failure.Operation != apitypes.DiskOperationPublish || disk.Failure.Message != "chunk upload refused" {
		t.Fatalf("disk after a failed publish: %+v err=%v", disk.Failure, err)
	}
	if _, err := s.RecordDiskGeneration(ctx, f.host, second, next.Disk, next.Token, generation(3), 100); err != nil {
		t.Fatal(err)
	}
	if disk, _ := s.GetDisk(ctx, f.ws, "root"); disk.Failure != nil {
		t.Fatalf("a published generation left the failure %+v", disk.Failure)
	}
}
