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

// TestStaleHolderCannotReplaceARecordedManifest: manifest keys carry their
// digest, so a key naming a generation without it is refused.
func TestStaleHolderCannotReplaceARecordedManifest(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, diskSpec)
	container := f.container()
	lease, err := f.storage.AcquireDisk(ctx, f.host, container, "root")
	if err != nil {
		t.Fatal(err)
	}
	g := generation(lease.Disk, 1, 0)
	g.ManifestKey = fmt.Sprintf("disks/%s/manifests/%012d.json", lease.Disk, 1)
	var bad *InvalidError
	if err := f.storage.RecordDiskGeneration(ctx, f.host, container, lease.Disk, lease.Token, g); !errors.As(err, &bad) {
		t.Fatalf("manifest key without its digest: %v", err)
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
