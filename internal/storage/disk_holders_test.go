package storage_test

import (
	"errors"
	"fmt"
	"slices"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	. "github.com/AmbientWare/lazycloud/internal/storage"
)

func (f *fixture) secondHost() compute.HostID {
	f.t.Helper()
	var host uuid.UUID
	f.exec(`insert into hosts (name, token_hash, state, cpu_millis, memory_bytes) values ('h2', sha256(gen_random_uuid()::text::bytea), 'online', 1000, 1000) returning id`, &host)
	return compute.HostID(host)
}

func (f *fixture) containerOn(host compute.HostID) uuid.UUID {
	f.t.Helper()
	var id uuid.UUID
	f.exec(`insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes)
		values ($1, $2, 'ready', $3, 1, 1000, 1000) returning id`, &id, uuid.UUID(f.ws), f.release, uuid.UUID(host))
	return id
}

// TestGenerationsNameTheirIndexBySHA256: a generation's index key is built
// from its sha256, so one that is not a sha256 is refused.
func TestGenerationsNameTheirIndexBySHA256(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, diskSpec)
	container := f.container()
	lease, err := f.storage.AcquireDisk(ctx, f.host, container, "root")
	if err != nil {
		t.Fatal(err)
	}
	var bad *InvalidError
	g := DiskGeneration{Generation: 1, IndexSHA256: "../../volumes/x"}
	if _, err := f.storage.RecordDiskGeneration(ctx, f.host, container, lease.Disk, lease.Token, g, 1); !errors.As(err, &bad) {
		t.Fatalf("an index named by a path: %v", err)
	}
}

// TestHolderOnALostHostReleasesTheDisk: a holder that stopped normally but
// never released keeps the disk only while its host can still release it.
func TestHolderOnALostHostReleasesTheDisk(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, diskSpec)
	s := f.storage
	first := f.container()
	if _, err := s.AcquireDisk(ctx, f.host, first, "root"); err != nil {
		t.Fatal(err)
	}
	// An attached disk names the workload holding it.
	if disk, err := s.GetDisk(ctx, f.ws, "root"); err != nil || disk.Holder == nil ||
		*disk.Holder != (apitypes.WorkloadRef{App: "app", Kind: apitypes.WorkloadKindFunction, Name: "fn"}) {
		t.Fatalf("disk holder: %+v err=%v", disk, err)
	}
	f.stop(first, "stopped")

	// The stopped holder still needs its grant for the final publish.
	workspaces, err := s.HostMountWorkspaces(ctx, f.host)
	if err != nil || !slices.Contains(workspaces, f.ws) {
		t.Fatalf("grant workspaces of a saving holder's host: %v err=%v", workspaces, err)
	}

	other := f.secondHost()
	second := f.containerOn(other)
	var held *ConflictError
	if _, err := s.AcquireDisk(ctx, other, second, "root"); !errors.As(err, &held) {
		t.Fatalf("acquire while the first host can still release: %v", err)
	}
	if _, err := f.pool.Exec(ctx, `update hosts set state = 'lost' where id = $1`, uuid.UUID(f.host)); err != nil {
		t.Fatal(err)
	}
	if disk, err := s.GetDisk(ctx, f.ws, "root"); err != nil || disk.Status != apitypes.Detached {
		t.Fatalf("disk after its holder's host was lost: %+v err=%v", disk, err)
	}
	if _, err := s.AcquireDisk(ctx, other, second, "root"); err != nil {
		t.Fatalf("acquire after the first host was lost: %v", err)
	}
}

// TestDeletingTheHolderContainerEndsTheLease: the lease's holder and token
// clear together, so the row check holds.
func TestDeletingTheHolderContainerEndsTheLease(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, diskSpec)
	container := f.container()
	if _, err := f.storage.AcquireDisk(ctx, f.host, container, "root"); err != nil {
		t.Fatal(err)
	}
	if _, err := f.pool.Exec(ctx, `delete from containers where id = $1`, container); err != nil {
		t.Fatalf("delete the holder container: %v", err)
	}
	if disk, err := f.storage.GetDisk(ctx, f.ws, "root"); err != nil || disk.Status != apitypes.Detached || disk.HolderContainerId != nil {
		t.Fatalf("disk after its holder was deleted: %+v err=%v", disk, err)
	}
}

// TestSupersededUploadsGoToTheHolder: a generation a container uploads after
// losing the disk is handed to the holder that records the next one, also
// on a replay, until it collects its index; the recorded index stays
// uncollectable.
func TestSupersededUploadsGoToTheHolder(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, diskSpec)
	s := f.storage
	first := f.container()
	lease, err := s.AcquireDisk(ctx, f.host, first, "root")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, generation(1), 100); err != nil {
		t.Fatal(err)
	}
	f.stop(first, "host_lost")
	second := f.container()
	next, err := s.AcquireDisk(ctx, f.host, second, "root")
	if err != nil {
		t.Fatal(err)
	}
	lost := DiskGeneration{Generation: 2, IndexSHA256: fmt.Sprintf("%064x", 0xbad)}
	if _, err := s.RecordDiskGeneration(ctx, f.host, first, lease.Disk, lease.Token, lost, 100); !errors.Is(err, ErrStaleLease) {
		t.Fatalf("an upload after losing the disk: %v", err)
	}
	// A replay, as after a lost reply, returns them again.
	for range 2 {
		orphans, err := s.RecordDiskGeneration(ctx, f.host, second, next.Disk, next.Token, generation(2), 100)
		if err != nil || !slices.Equal(orphans, []DiskGeneration{lost}) {
			t.Fatalf("the holder's next generation returned orphans %v, %v; want the lost upload", orphans, err)
		}
	}
	key := func(g DiskGeneration) string {
		return fmt.Sprintf("disks/%s/manifests/%012d-%s", next.Disk, g.Generation, g.IndexSHA256)
	}
	var bad *InvalidError
	if err := s.CollectDisk(ctx, f.host, second, next.Disk, next.Token, 2, []string{key(generation(2))}, 0); !errors.As(err, &bad) {
		t.Fatalf("collecting the recorded index: %v", err)
	}
	if err := s.CollectDisk(ctx, f.host, second, next.Disk, next.Token, 2, []string{key(lost)}, 0); err != nil {
		t.Fatalf("collecting the orphaned index: %v", err)
	}
	if orphans, err := s.RecordDiskGeneration(ctx, f.host, second, next.Disk, next.Token, generation(3), 100); err != nil || len(orphans) != 0 {
		t.Fatalf("a later generation returned orphans %v, %v", orphans, err)
	}
}
