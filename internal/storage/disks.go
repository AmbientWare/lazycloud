package storage

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Disk size bounds. Sizes are whole 4 KiB blocks.
const (
	MinDiskBytes   = 1 << 30
	MaxDiskBytes   = 1 << 40
	diskBlockBytes = 4096
)

// ErrStaleLease means a disk call came from a container that no longer
// holds the disk, or whose container has stopped.
var ErrStaleLease = errors.New("the container does not hold the disk")

// diskHeld reports whether the holder still keeps the disk: its container
// has not stopped, or it stopped without releasing and its host was not lost.
func diskHeld(holder *uuid.UUID, state, stopReason *string, released bool) bool {
	if holder == nil || state == nil {
		return false
	}
	if *state != "stopped" {
		return true
	}
	return !released && (stopReason == nil || *stopReason != "host_lost")
}

func diskStatus(holder *uuid.UUID, state, stopReason *string, released bool) apitypes.DiskStatus {
	switch {
	case !diskHeld(holder, state, stopReason, released):
		return apitypes.Detached
	case *state == "stopped":
		return apitypes.Saving
	}
	return apitypes.Attached
}

// DiskGeneration is one published generation of a disk chain.
type DiskGeneration struct {
	Generation     int64
	ManifestKey    string
	ManifestSHA256 string
}

// DiskLease is a container's hold on a disk. Token fences every publish.
type DiskLease struct {
	Disk       uuid.UUID
	Workspace  identity.WorkspaceID
	SizeBytes  int64
	Generation int64
	Token      []byte
	// Chain runs from the newest parentless generation to the newest, base
	// first.
	Chain  []DiskGeneration
	Bucket string
}

// AcquireDisk gives container the disk its release declares by name,
// creating the disk on first use and growing it to the declared size. The
// same container acquiring again gets its lease back. Another holder that
// still keeps the disk is a conflict; the host retries.
func (s *Storage) AcquireDisk(ctx context.Context, host compute.HostID, container uuid.UUID, name string) (DiskLease, error) {
	declared, err := s.queries.DeclaredDisk(ctx, DeclaredDiskParams{ContainerID: container, HostID: hostRef(host), Name: name})
	if errors.Is(err, pgx.ErrNoRows) {
		return DiskLease{}, ErrStaleLease
	}
	if err != nil {
		return DiskLease{}, fmt.Errorf("read declared disk: %w", err)
	}
	if declared.SizeBytes < MinDiskBytes || declared.SizeBytes > MaxDiskBytes || declared.SizeBytes%diskBlockBytes != 0 {
		return DiskLease{}, invalid("disk %s size %d is not a multiple of 4096 between 1Gi and 1Ti", name, declared.SizeBytes)
	}
	workspace := identity.WorkspaceID(declared.WorkspaceID)
	bucket, err := s.workspaceBucket(ctx, workspace)
	if err != nil {
		return DiskLease{}, err
	}
	lease := DiskLease{Workspace: workspace, Bucket: bucket}
	err = pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		q := s.queries.WithTx(tx)
		if err := q.InsertDisk(ctx, InsertDiskParams{WorkspaceID: declared.WorkspaceID, Name: name, SizeBytes: declared.SizeBytes}); err != nil {
			return fmt.Errorf("create disk: %w", err)
		}
		disk, err := q.LockActiveDisk(ctx, LockActiveDiskParams{WorkspaceID: declared.WorkspaceID, Name: name})
		if err != nil {
			return fmt.Errorf("lock disk: %w", err)
		}
		token := disk.LeaseToken
		mine := disk.HolderContainerID != nil && *disk.HolderContainerID == container
		if !mine {
			if diskHeld(disk.HolderContainerID, disk.HolderState, disk.HolderStopReason, disk.ReleasedAt != nil) {
				return conflict("disk %s is held by container %s", name, *disk.HolderContainerID)
			}
			token = make([]byte, 32)
			_, _ = rand.Read(token)
			if err := q.TakeDiskLease(ctx, TakeDiskLeaseParams{ID: disk.ID, ContainerID: &container, LeaseToken: token}); err != nil {
				return fmt.Errorf("take disk lease: %w", err)
			}
		}
		// Disks grow and never shrink; a smaller declaration keeps the size.
		size := max(disk.SizeBytes, declared.SizeBytes)
		if size > disk.SizeBytes {
			if err := q.GrowDisk(ctx, GrowDiskParams{ID: disk.ID, SizeBytes: size}); err != nil {
				return fmt.Errorf("grow disk: %w", err)
			}
		}
		chain, err := q.DiskChain(ctx, disk.ID)
		if err != nil {
			return fmt.Errorf("read disk chain: %w", err)
		}
		lease.Disk, lease.SizeBytes, lease.Generation, lease.Token = disk.ID, size, disk.Generation, token
		lease.Chain = make([]DiskGeneration, len(chain))
		for n, g := range chain {
			lease.Chain[n] = DiskGeneration{Generation: g.Generation, ManifestKey: g.ManifestKey, ManifestSHA256: g.ManifestSha256}
		}
		return nil
	})
	if err != nil {
		return DiskLease{}, fmt.Errorf("acquire disk %s: %w", name, err)
	}
	return lease, nil
}

// PublishedGeneration is a generation a host uploaded.
type PublishedGeneration struct {
	Generation, ParentGeneration int64
	ManifestKey, ManifestSHA256  string
	AddedBytes                   int64
	Flat                         bool
}

// diskLease holds a fenced lease for the length of fn.
func (s *Storage) withDiskLease(ctx context.Context, host compute.HostID, container, disk uuid.UUID, token []byte, fn func(*Queries, LockLeasedDiskRow) error) error {
	return pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error { //nolint:wrapcheck // Callers wrap.
		q := s.queries.WithTx(tx)
		row, err := q.LockLeasedDisk(ctx, LockLeasedDiskParams{ID: disk, ContainerID: &container, LeaseToken: token, HostID: hostRef(host)})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrStaleLease
		}
		if err != nil {
			return fmt.Errorf("lock disk lease: %w", err)
		}
		return fn(q, row)
	})
}

// RecordDiskGeneration accepts the next generation from the lease holder.
// A replay of the recorded generation with the same manifest succeeds, so
// the host can retry after a lost reply.
func (s *Storage) RecordDiskGeneration(ctx context.Context, host compute.HostID, container, disk uuid.UUID, token []byte, g PublishedGeneration) error {
	err := s.withDiskLease(ctx, host, container, disk, token, func(q *Queries, row LockLeasedDiskRow) error {
		if g.Generation <= row.Generation {
			recorded, err := q.DiskGeneration(ctx, DiskGenerationParams{DiskID: disk, Generation: g.Generation})
			if err == nil && subtle.ConstantTimeCompare([]byte(recorded), []byte(g.ManifestSHA256)) == 1 {
				return nil
			}
			return conflict("generation %d is already recorded", g.Generation)
		}
		if g.Generation != row.Generation+1 || (g.ParentGeneration != 0 && g.ParentGeneration != row.Generation) {
			return conflict("generation %d with parent %d does not follow %d", g.Generation, g.ParentGeneration, row.Generation)
		}
		want := fmt.Sprintf("disks/%s/manifests/%012d.json", disk, g.Generation)
		if g.ManifestKey != want {
			return invalid("manifest key %q, want %q", g.ManifestKey, want)
		}
		if err := q.InsertDiskGeneration(ctx, InsertDiskGenerationParams{
			DiskID: disk, Generation: g.Generation, ParentGeneration: g.ParentGeneration,
			ManifestKey: g.ManifestKey, ManifestSha256: g.ManifestSHA256, Flat: g.Flat,
		}); err != nil {
			return fmt.Errorf("record generation: %w", err)
		}
		return q.AdvanceDisk(ctx, AdvanceDiskParams{ID: disk, Generation: g.Generation, AddedBytes: max(g.AddedBytes, 0)})
	})
	if err != nil {
		return fmt.Errorf("record disk generation: %w", err)
	}
	return nil
}

// RecordDiskCollection records bytes a collection removed and forgets
// generations older than the newest parentless one, which no restore needs.
func (s *Storage) RecordDiskCollection(ctx context.Context, host compute.HostID, container, disk uuid.UUID, token []byte, removedBytes, base int64) error {
	err := s.withDiskLease(ctx, host, container, disk, token, func(q *Queries, _ LockLeasedDiskRow) error {
		if err := q.ShrinkDiskStored(ctx, ShrinkDiskStoredParams{ID: disk, ContainerID: &container, LeaseToken: token, RemovedBytes: max(removedBytes, 0)}); err != nil {
			return fmt.Errorf("record collection: %w", err)
		}
		return q.DeleteDiskGenerationsBefore(ctx, DeleteDiskGenerationsBeforeParams{DiskID: disk, Generation: base})
	})
	if err != nil {
		return fmt.Errorf("record disk collection: %w", err)
	}
	return nil
}

// ReleaseDisk ends a lease after the holder's final publish. It may follow
// the container's stop, so it checks only the token.
func (s *Storage) ReleaseDisk(ctx context.Context, container, disk uuid.UUID, token []byte) error {
	n, err := s.queries.ReleaseDisk(ctx, ReleaseDiskParams{ID: disk, ContainerID: &container, LeaseToken: token})
	if err != nil {
		return fmt.Errorf("release disk: %w", err)
	}
	if n == 0 {
		return ErrStaleLease
	}
	return nil
}

func diskOut(row ListDisksRow) apitypes.Disk {
	status := diskStatus(row.HolderContainerID, row.HolderState, row.HolderStopReason, row.ReleasedAt != nil)
	out := apitypes.Disk{
		Id: row.ID, Name: row.Name, SizeBytes: row.SizeBytes, StoredBytes: row.StoredBytes, Generation: row.Generation,
		Status: status, CreatedAt: row.CreatedAt, UpdatedAt: row.UpdatedAt,
	}
	if status != apitypes.Detached {
		out.HolderContainerId = row.HolderContainerID
	}
	return out
}

// ListDisks returns a page of active disks by name.
func (s *Storage) ListDisks(ctx context.Context, workspace identity.WorkspaceID, cursor string, limit int) (apitypes.DiskPage, error) {
	rows, err := s.queries.ListDisks(ctx, ListDisksParams{WorkspaceID: uuid.UUID(workspace), After: cursor, MaxRows: int32(limit)}) //nolint:gosec // The schema caps limit.
	if err != nil {
		return apitypes.DiskPage{}, fmt.Errorf("list disks: %w", err)
	}
	page := apitypes.DiskPage{Disks: make([]apitypes.Disk, len(rows))}
	for n, row := range rows {
		page.Disks[n] = diskOut(row)
	}
	if len(rows) == limit {
		page.NextCursor = &rows[len(rows)-1].Name
	}
	return page, nil
}

// GetDisk returns the active disk with name.
func (s *Storage) GetDisk(ctx context.Context, workspace identity.WorkspaceID, name string) (apitypes.Disk, error) {
	row, err := s.queries.ActiveDisk(ctx, ActiveDiskParams{WorkspaceID: uuid.UUID(workspace), Name: name})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.Disk{}, ErrNotFound
	}
	if err != nil {
		return apitypes.Disk{}, fmt.Errorf("read disk: %w", err)
	}
	return diskOut(ListDisksRow(row)), nil
}

// DeleteDisk marks the disk deleting, which frees its name; the sweep
// removes its bytes. It is refused while a holder keeps the disk.
func (s *Storage) DeleteDisk(ctx context.Context, workspace identity.WorkspaceID, name string) error {
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		q := s.queries.WithTx(tx)
		disk, err := q.LockActiveDisk(ctx, LockActiveDiskParams{WorkspaceID: uuid.UUID(workspace), Name: name})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock disk: %w", err)
		}
		if diskHeld(disk.HolderContainerID, disk.HolderState, disk.HolderStopReason, disk.ReleasedAt != nil) {
			return conflict("stop container %s before deleting disk %s", *disk.HolderContainerID, name)
		}
		return q.MarkDiskDeleting(ctx, disk.ID)
	})
	if err != nil {
		return fmt.Errorf("delete disk %s: %w", name, err)
	}
	return nil
}
