package storage

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"errors"
	"fmt"
	"regexp"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
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

// holder is the container holding a disk and what became of it.
type holder struct {
	container                    *uuid.UUID
	state, stopReason, hostState *string
	released                     bool
}

// held reports whether the holder still keeps the disk: its container has
// not stopped, or it stopped without releasing and its host can still
// release it. A host that was lost or retired never will.
func (h holder) held() bool {
	if h.container == nil || h.state == nil {
		return false
	}
	if *h.state != "stopped" {
		return true
	}
	if h.released || (h.stopReason != nil && *h.stopReason == "host_lost") {
		return false
	}
	return h.hostState == nil || (*h.hostState != "lost" && *h.hostState != "retired")
}

func (h holder) status() apitypes.DiskStatus {
	switch {
	case !h.held():
		return apitypes.Detached
	case *h.state == "stopped":
		return apitypes.Saving
	}
	return apitypes.Attached
}

func lockedHolder(d LockActiveDiskRow) holder {
	return holder{d.HolderContainerID, d.HolderState, d.HolderStopReason, d.HolderHostState, d.ReleasedAt != nil}
}

// DiskGeneration is one published generation of a disk: its stored index
// and the index's sha256.
type DiskGeneration struct {
	Generation     int64
	ManifestKey    string
	ManifestSHA256 string
}

// DiskLease is a container's hold on a disk. Token fences every publish.
type DiskLease struct {
	Disk      uuid.UUID
	Workspace identity.WorkspaceID
	SizeBytes int64
	Token     []byte
	// Newest is the newest published generation; nil for a disk never
	// published.
	Newest *DiskGeneration
	Bucket string
}

// DiskGrowth is what declaring disks would do to a workspace's disks.
type DiskGrowth struct {
	// TotalBytes is what the workspace's live disks would declare, counted
	// as AcquireDisk counts them against the plan.
	TotalBytes int64
	// Growing are the declared disks that would be created or grown.
	Growing []string
}

// DeclaredDiskGrowth is what declared, sizes by disk name, would do to the
// workspace's live disks. Disks grow and never shrink, so a declaration at
// or below a disk's size changes nothing.
func DeclaredDiskGrowth(ctx context.Context, db DBTX, workspace uuid.UUID, declared map[string]int64) (DiskGrowth, error) {
	params := DeclaredDiskGrowthParams{WorkspaceID: workspace}
	for name, size := range declared {
		params.Names = append(params.Names, name)
		params.Sizes = append(params.Sizes, size)
	}
	row, err := New(db).DeclaredDiskGrowth(ctx, params)
	if err != nil {
		return DiskGrowth{}, fmt.Errorf("read declared disk growth: %w", err)
	}
	return DiskGrowth(row), nil
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
	store, err := s.workspaceStore(ctx, workspace)
	if err != nil {
		return DiskLease{}, err
	}
	lease := DiskLease{Workspace: workspace, Bucket: store.name}
	err = pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		q := s.queries.WithTx(tx)
		// Creating or growing a disk is held to the plan's disk allowance.
		sizes, err := q.WorkspaceDiskBytes(ctx, WorkspaceDiskBytesParams{WorkspaceID: declared.WorkspaceID, Name: name})
		if err != nil {
			return fmt.Errorf("read declared disk sizes: %w", err)
		}
		if declared.SizeBytes > sizes.Own {
			if err := billing.AdmitDisk(ctx, tx, declared.WorkspaceID, sizes.Others+declared.SizeBytes); err != nil {
				return err
			}
		}
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
			if lockedHolder(disk).held() {
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
		lease.Disk, lease.SizeBytes, lease.Token = disk.ID, size, token
		if disk.Generation == 0 {
			return nil
		}
		newest, err := q.NewestDiskGeneration(ctx, disk.ID)
		if err != nil {
			return fmt.Errorf("read newest disk generation: %w", err)
		}
		lease.Newest = &DiskGeneration{Generation: newest.Generation, ManifestKey: newest.ManifestKey, ManifestSHA256: newest.ManifestSha256}
		return nil
	})
	if err != nil {
		return DiskLease{}, fmt.Errorf("acquire disk %s: %w", name, err)
	}
	return lease, nil
}

// PublishedGeneration is a generation a host uploaded.
type PublishedGeneration struct {
	Generation                  int64
	ManifestKey, ManifestSHA256 string
	AddedBytes                  int64
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
		if g.Generation != row.Generation+1 {
			return conflict("generation %d does not follow %d", g.Generation, row.Generation)
		}
		// The key carries the index digest, so no upload can replace a
		// recorded index.
		want := fmt.Sprintf("%smanifests/%012d-%s", diskPrefix(disk), g.Generation, g.ManifestSHA256)
		if g.ManifestKey != want {
			return invalid("manifest key %q, want %q", g.ManifestKey, want)
		}
		if err := q.InsertDiskGeneration(ctx, InsertDiskGenerationParams{
			DiskID: disk, Generation: g.Generation, ManifestKey: g.ManifestKey, ManifestSha256: g.ManifestSHA256,
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

// maxCollectKeys bounds the keys one CollectDisk call deletes.
const maxCollectKeys = 1000

// CollectDisk deletes keys, objects of the disk that generation, a recorded
// one, no longer reads, and forgets older generations. The keys are deleted
// while the lease's row lock is held: no other container can take the disk
// or record a generation until they are gone, and a holder that lost the
// lease deletes nothing. Keys outside the disk's indexes below generation
// and its frames are refused.
func (s *Storage) CollectDisk(ctx context.Context, host compute.HostID, container, disk uuid.UUID, token []byte, base int64, keys []string, removedBytes int64) error {
	if len(keys) > maxCollectKeys {
		return invalid("collect at most %d keys at once, got %d", maxCollectKeys, len(keys))
	}
	for _, key := range keys {
		if !collectable(disk, base, key) {
			return invalid("key %q is not an index below generation %d or a frame of disk %s", key, base, disk)
		}
	}
	err := s.withDiskLease(ctx, host, container, disk, token, func(q *Queries, row LockLeasedDiskRow) error {
		if base > row.Generation {
			return invalid("generation %d is past the recorded generation %d", base, row.Generation)
		}
		if len(keys) > 0 {
			if row.Bucket == nil || row.Region == nil {
				return invalid("disk %s has no workspace bucket", disk)
			}
			store, err := s.storeOf(ctx, *row.Bucket, *row.Region, row.ConnectionID)
			if err != nil {
				return err
			}
			if err := s.deleteKeys(ctx, store, keys); err != nil {
				return fmt.Errorf("delete collected objects: %w", err)
			}
		}
		if err := q.ShrinkDiskStored(ctx, ShrinkDiskStoredParams{ID: disk, ContainerID: &container, LeaseToken: token, RemovedBytes: max(removedBytes, 0)}); err != nil {
			return fmt.Errorf("record collection: %w", err)
		}
		return q.DeleteDiskGenerationsBefore(ctx, DeleteDiskGenerationsBeforeParams{DiskID: disk, Generation: base})
	})
	if err != nil {
		return fmt.Errorf("collect disk: %w", err)
	}
	return nil
}

var (
	collectIndex = regexp.MustCompile(`^manifests/(\d{12})-[0-9a-f]{64}$`)
	collectFrame = regexp.MustCompile(`^frames/[0-9a-f]{64}$`)
)

// collectable reports whether key names an index of disk below base or one
// of its frames, as the disk engine stores them.
func collectable(disk uuid.UUID, base int64, key string) bool {
	rest, ok := strings.CutPrefix(key, diskPrefix(disk))
	if !ok {
		return false
	}
	if m := collectIndex.FindStringSubmatch(rest); m != nil {
		generation, err := strconv.ParseInt(m[1], 10, 64)
		return err == nil && generation < base
	}
	return collectFrame.MatchString(rest)
}

// ReleaseDisk ends a lease after the holder's final publish, clearing any
// failure it recorded. It may follow the container's stop, so it checks only
// the token.
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

// maxFailureMessage bounds a recorded failure's message, in bytes.
const maxFailureMessage = 4096

// DiskFailure is why a holder's publish or release failed.
type DiskFailure struct {
	Operation apitypes.DiskOperation
	Message   string
}

// RecordDiskFailure records the holder's latest failure, or clears it when
// failure is nil, until the lease is released. A longer message keeps its
// first maxFailureMessage bytes.
func (s *Storage) RecordDiskFailure(ctx context.Context, container, disk uuid.UUID, token []byte, failure *DiskFailure) error {
	params := SetDiskFailureParams{ID: disk, ContainerID: &container, LeaseToken: token}
	if failure != nil {
		if !failure.Operation.Valid() {
			return invalid("unknown disk operation %q", failure.Operation)
		}
		message := strings.ToValidUTF8(failure.Message[:min(len(failure.Message), maxFailureMessage)], "")
		params.Operation, params.Message = (*string)(&failure.Operation), &message
	}
	n, err := s.queries.SetDiskFailure(ctx, params)
	if err != nil {
		return fmt.Errorf("record disk failure: %w", err)
	}
	if n == 0 {
		return ErrStaleLease
	}
	return nil
}

func diskOut(row ListDisksRow) apitypes.Disk {
	status := holder{row.HolderContainerID, row.HolderState, row.HolderStopReason, row.HolderHostState, row.ReleasedAt != nil}.status()
	out := apitypes.Disk{
		Id: row.ID, Name: row.Name, SizeBytes: row.SizeBytes, StoredBytes: row.StoredBytes, Generation: row.Generation,
		Status: status, CreatedAt: row.CreatedAt, UpdatedAt: row.UpdatedAt,
	}
	if row.FailedOperation != nil && row.FailureMessage != nil && row.FailedAt != nil {
		out.Failure = &apitypes.DiskFailure{Operation: apitypes.DiskOperation(*row.FailedOperation), Message: *row.FailureMessage, FailedAt: *row.FailedAt}
	}
	if status != apitypes.Detached {
		out.HolderContainerId = row.HolderContainerID
		if row.HolderApp != nil && row.HolderKind != nil && row.HolderWorkload != nil {
			out.Holder = &apitypes.WorkloadRef{App: *row.HolderApp, Kind: apitypes.WorkloadKind(*row.HolderKind), Name: *row.HolderWorkload}
		}
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
		if lockedHolder(disk).held() {
			return conflict("stop container %s before deleting disk %s", *disk.HolderContainerID, name)
		}
		return q.MarkDiskDeleting(ctx, disk.ID)
	})
	if err != nil {
		return fmt.Errorf("delete disk %s: %w", name, err)
	}
	return nil
}
