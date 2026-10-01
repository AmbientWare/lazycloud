package storage_test

import (
	"errors"
	"fmt"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	. "github.com/AmbientWare/lazycloud/internal/storage"
)

const diskSpec = `{"name":"fn","disks":[{"name":"root","size_bytes":2147483648,"mount_path":"/"}]}`

func generation(disk uuid.UUID, n, parent int64) PublishedGeneration {
	return PublishedGeneration{
		Generation: n, ParentGeneration: parent, ManifestKey: fmt.Sprintf("disks/%s/manifests/%012d.json", disk, n),
		ManifestSHA256: fmt.Sprintf("%064d", n), AddedBytes: 100,
	}
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
	if lease.SizeBytes != 2<<30 || lease.Generation != 0 || len(lease.Chain) != 0 || lease.Bucket == "" {
		t.Fatalf("first lease: %+v", lease)
	}
	again, err := s.AcquireDisk(ctx, f.host, first, "root")
	if err != nil || string(again.Token) != string(lease.Token) {
		t.Fatalf("reacquire by the holder: err=%v", err)
	}
	if _, err := s.AcquireDisk(ctx, f.host, first, "undeclared"); !errors.Is(err, ErrStaleLease) {
		t.Fatalf("undeclared disk: %v", err)
	}

	if err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(lease.Disk, 1, 0)); err != nil {
		t.Fatal(err)
	}
	// A replay of the recorded generation is accepted; a skip is not.
	if err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(lease.Disk, 1, 0)); err != nil {
		t.Fatalf("replay: %v", err)
	}
	var out *ConflictError
	if err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(lease.Disk, 3, 1)); !errors.As(err, &out) {
		t.Fatalf("skipped generation: %v", err)
	}
	if err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, []byte("forged"), generation(lease.Disk, 2, 1)); !errors.Is(err, ErrStaleLease) {
		t.Fatalf("forged token: %v", err)
	}
	if err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(lease.Disk, 2, 1)); err != nil {
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
	if err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(lease.Disk, 3, 2)); err != nil {
		t.Fatalf("final publish after stop: %v", err)
	}
	if err := s.ReleaseDisk(ctx, first, lease.Disk, lease.Token); err != nil {
		t.Fatal(err)
	}
	if err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(lease.Disk, 4, 3)); !errors.Is(err, ErrStaleLease) {
		t.Fatalf("publish after release: %v", err)
	}

	next, err := s.AcquireDisk(ctx, f.host, second, "root")
	if err != nil || string(next.Token) == string(lease.Token) || len(next.Chain) != 3 || next.Chain[2].Generation != 3 {
		t.Fatalf("lease after release: %+v err=%v", next, err)
	}
	if err := s.ReleaseDisk(ctx, first, lease.Disk, lease.Token); !errors.Is(err, ErrStaleLease) {
		t.Fatalf("release by a replaced holder: %v", err)
	}

	// A flat generation starts a new chain.
	flat := generation(next.Disk, 4, 0)
	flat.Flat = true
	if err := s.RecordDiskGeneration(ctx, f.host, second, next.Disk, next.Token, flat); err != nil {
		t.Fatal(err)
	}
	if err := s.RecordDiskCollection(ctx, f.host, second, next.Disk, next.Token, 150, 4); err != nil {
		t.Fatal(err)
	}

	// Host loss ends the hold without a release.
	f.stop(second, "host_lost")
	third := f.container()
	last, err := s.AcquireDisk(ctx, f.host, third, "root")
	if err != nil || len(last.Chain) != 1 || last.Chain[0].Generation != 4 {
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
