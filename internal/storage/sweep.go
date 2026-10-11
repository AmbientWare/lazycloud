package storage

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

const (
	// sweepBatch bounds the items one sweep step takes.
	sweepBatch = 100
	// measureEvery is how often a volume's stored bytes are recounted.
	measureEvery = 10 * time.Minute
)

// SweepResult counts what one sweep removed or measured.
type SweepResult struct {
	Artifacts, Volumes, Disks, MapEntries, Grants, Measured, Orphans int
}

// Sweep runs one bounded pass of storage retention and cleanup: expired and
// abandoned artifacts, deleted volumes and disks, expired map entries,
// expired provider keys, stale volume sizes and objects no row owns. No
// step holds a transaction across object store calls, so several
// schedulers can sweep at once; their work overlaps only in deletes that
// are idempotent. One item's failure is logged and left for the next pass.
func (s *Storage) Sweep(ctx context.Context, logger *slog.Logger) (SweepResult, error) {
	var errs []error
	step := func(name string, n int, err error) int {
		if err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", name, err))
		}
		return n
	}
	var out SweepResult
	n, err := s.sweepArtifacts(ctx, logger)
	out.Artifacts = step("artifacts", n, err)
	n, err = s.sweepVolumes(ctx, logger)
	out.Volumes = step("volumes", n, err)
	n, err = s.sweepDisks(ctx, logger)
	out.Disks = step("disks", n, err)
	swept, err := s.queries.SweepExpiredMapEntries(ctx, sweepBatch*10)
	out.MapEntries = step("map entries", int(swept), err)
	n, err = s.sweepGrants(ctx, logger)
	out.Grants = step("grants", n, err)
	n, err = s.measureVolumes(ctx, logger)
	out.Measured = step("volume sizes", n, err)
	n, err = s.sweepOrphans(ctx, logger)
	out.Orphans = step("orphans", n, err)
	return out, errors.Join(errs...)
}

// sweepArtifacts deletes expired artifacts and uploads abandoned for a day.
// A row is deleted only once its bytes are gone.
func (s *Storage) sweepArtifacts(ctx context.Context, logger *slog.Logger) (int, error) {
	rows, err := s.queries.ExpiredArtifacts(ctx, sweepBatch)
	if err != nil {
		return 0, fmt.Errorf("read expired artifacts: %w", err)
	}
	keys := make([]string, 0, len(rows))
	ids := map[string]uuid.UUID{}
	for _, row := range rows {
		key := artifactKey(identity.WorkspaceID(row.WorkspaceID), row.ID)
		if row.UploadID != nil {
			if err := s.abortMultipart(ctx, s.platform, key, *row.UploadID); err != nil {
				logger.WarnContext(ctx, "aborting an abandoned artifact upload failed", "artifact", row.ID.String(), "error", err)
				continue
			}
		}
		keys = append(keys, key)
		ids[key] = row.ID
	}
	failed, err := s.tryDeleteKeys(ctx, s.platform, keys)
	if err != nil {
		return 0, err
	}
	gone := make([]uuid.UUID, 0, len(keys))
	for _, key := range keys {
		if reason, ok := failed[key]; ok {
			logger.WarnContext(ctx, "deleting an expired artifact failed", "artifact", ids[key].String(), "error", reason)
			continue
		}
		gone = append(gone, ids[key])
	}
	if err := s.queries.DeleteExpiredArtifactRows(ctx, gone); err != nil {
		return 0, fmt.Errorf("delete artifact rows: %w", err)
	}
	return len(gone), nil
}

// sweepVolumes removes deleted volumes' files a chunk at a time and each
// row once its prefix is empty.
func (s *Storage) sweepVolumes(ctx context.Context, logger *slog.Logger) (int, error) {
	rows, err := s.queries.DeletingVolumes(ctx, sweepBatch)
	if err != nil {
		return 0, fmt.Errorf("read deleted volumes: %w", err)
	}
	return sweepDeleted(ctx, s, logger, "volume", rows, volumePrefix, s.queries.DeleteVolumeRow), nil
}

func (s *Storage) sweepDisks(ctx context.Context, logger *slog.Logger) (int, error) {
	rows, err := s.queries.DeletingDisks(ctx, sweepBatch)
	if err != nil {
		return 0, fmt.Errorf("read deleted disks: %w", err)
	}
	return sweepDeleted(ctx, s, logger, "disk", rows, diskPrefix, s.queries.DeleteDiskRow), nil
}

func diskPrefix(disk uuid.UUID) string { return "disks/" + disk.String() + "/" }

// sweepDeleted deletes a chunk of the files of each deleted volume or disk
// in rows, under prefix in its workspace bucket, and its row once they are
// gone; a nil bucket means the workspace never had one. It returns how many
// rows went. An item's failure is logged and left for the next pass.
func sweepDeleted[R DeletingVolumesRow | DeletingDisksRow](ctx context.Context, s *Storage, logger *slog.Logger, kind string, rows []R,
	prefix func(uuid.UUID) string, deleteRow func(context.Context, uuid.UUID) error,
) int {
	removed := 0
	for _, r := range rows {
		row := DeletingDisksRow(r)
		empty, err := true, error(nil)
		if row.Bucket != nil && row.Region != nil {
			var store bucketClient
			if store, err = s.storeOf(ctx, *row.Bucket, *row.Region, row.ConnectionID); err == nil {
				empty, err = s.deletePrefixChunk(ctx, store, prefix(row.ID))
			}
		}
		if err == nil && empty {
			if err = deleteRow(ctx, row.ID); err == nil {
				removed++
			}
		}
		if err != nil {
			logger.WarnContext(ctx, "removing a deleted "+kind+" failed", kind, row.ID.String(), "error", err)
		}
	}
	return removed
}

// sweepGrants deletes expired provider keys.
func (s *Storage) sweepGrants(ctx context.Context, logger *slog.Logger) (int, error) {
	if s.buckets == nil {
		return 0, nil
	}
	keys, err := s.queries.ExpiredStorageGrants(ctx, sweepBatch)
	if err != nil {
		return 0, fmt.Errorf("read expired grants: %w", err)
	}
	n := 0
	for _, key := range keys {
		if err := s.buckets.revoke(ctx, key); err != nil {
			logger.WarnContext(ctx, "revoking an expired storage key failed", "error", err)
			continue
		}
		if err := s.queries.DeleteStorageGrant(ctx, key); err != nil {
			return n, fmt.Errorf("delete grant row: %w", err)
		}
		n++
	}
	return n, nil
}

// measureVolumes recounts the stored bytes of volumes not measured within
// measureEvery.
func (s *Storage) measureVolumes(ctx context.Context, logger *slog.Logger) (int, error) {
	rows, err := s.queries.VolumesToMeasure(ctx, VolumesToMeasureParams{EverySeconds: measureEvery.Seconds(), MaxRows: sweepBatch / 10})
	if err != nil {
		return 0, fmt.Errorf("read volumes to measure: %w", err)
	}
	n := 0
	for _, row := range rows {
		size, err := s.storedBytes(ctx, row.Bucket, row.Region, row.ConnectionID, volumePrefix(row.ID))
		if err != nil {
			logger.WarnContext(ctx, "measuring a volume failed", "volume", row.ID.String(), "error", err)
			continue
		}
		if err := s.queries.RecordVolumeSize(ctx, RecordVolumeSizeParams{ID: row.ID, SizeBytes: size}); err != nil {
			return n, fmt.Errorf("record volume size: %w", err)
		}
		n++
	}
	return n, nil
}

// storedBytes is the size of the objects under prefix in a workspace
// bucket.
func (s *Storage) storedBytes(ctx context.Context, bucket, region string, connection *uuid.UUID, prefix string) (int64, error) {
	store, err := s.storeOf(ctx, bucket, region, connection)
	if err != nil {
		return 0, err
	}
	var size int64
	err = s.eachObject(ctx, store, prefix, func(batch []objectInfo) error {
		for _, o := range batch {
			size += o.Size
		}
		return nil
	})
	return size, err
}

// orphanAge is how old an object without a row must be before it goes: an
// upload URL or a host key issued before its owner's delete stays valid
// that long.
const orphanAge = uploadLifetime + grantLifetime

// sweepOrphans checks one workspace an hour for objects no row owns: files
// under volumes/<id>/ or disks/<id>/ of its bucket and artifacts of the
// platform bucket whose rows are gone. Rows are written before any upload
// and ids are never reused, so a missing row means the owner was deleted.
func (s *Storage) sweepOrphans(ctx context.Context, logger *slog.Logger) (int, error) {
	claim, err := s.queries.ClaimOrphanCheck(ctx)
	if errors.Is(err, pgx.ErrNoRows) {
		return 0, nil
	}
	if err != nil {
		return 0, fmt.Errorf("claim an orphan check: %w", err)
	}
	store, err := s.storeOf(ctx, claim.Bucket, claim.Region, claim.ConnectionID)
	if err != nil {
		return 0, err
	}
	removed := 0
	for _, kind := range []struct {
		prefix string
		known  func(context.Context, []uuid.UUID) ([]uuid.UUID, error)
	}{{"volumes/", s.queries.KnownVolumes}, {"disks/", s.queries.KnownDisks}} {
		n, err := s.removeOrphanPrefixes(ctx, store, kind.prefix, kind.known)
		removed += n
		if err != nil {
			logger.WarnContext(ctx, "removing orphaned objects failed", "bucket", claim.Bucket, "prefix", kind.prefix, "error", err)
		}
	}
	n, err := s.removeOrphanArtifacts(ctx, identity.WorkspaceID(claim.WorkspaceID))
	removed += n
	if err != nil {
		logger.WarnContext(ctx, "removing orphaned artifacts failed", "workspace", claim.WorkspaceID.String(), "error", err)
	}
	return removed, nil
}

// removeOrphanPrefixes deletes, a chunk each, the <prefix><id>/ prefixes
// whose ids have no row.
func (s *Storage) removeOrphanPrefixes(ctx context.Context, bucket bucketClient, prefix string, known func(context.Context, []uuid.UUID) ([]uuid.UUID, error)) (int, error) {
	pages := s3.NewListObjectsV2Paginator(bucket.client, &s3.ListObjectsV2Input{Bucket: aws.String(bucket.name), Prefix: aws.String(prefix), Delimiter: aws.String("/")})
	removed := 0
	for pages.HasMorePages() {
		page, err := pages.NextPage(ctx)
		if err != nil {
			return removed, fmt.Errorf("list %s: %w", prefix, err)
		}
		ids := make([]uuid.UUID, 0, len(page.CommonPrefixes))
		for _, p := range page.CommonPrefixes {
			if id, err := uuid.Parse(strings.TrimSuffix(strings.TrimPrefix(aws.ToString(p.Prefix), prefix), "/")); err == nil {
				ids = append(ids, id)
			}
		}
		owned, err := known(ctx, ids)
		if err != nil {
			return removed, fmt.Errorf("read owners: %w", err)
		}
		live := map[uuid.UUID]bool{}
		for _, id := range owned {
			live[id] = true
		}
		for _, id := range ids {
			if live[id] {
				continue
			}
			if young, err := s.hasRecentObject(ctx, bucket, prefix+id.String()+"/"); err != nil || young {
				continue
			}
			if _, err := s.deletePrefixChunk(ctx, bucket, prefix+id.String()+"/"); err != nil {
				return removed, err
			}
			removed++
		}
	}
	return removed, nil
}

// hasRecentObject reports whether prefix holds an object newer than
// orphanAge.
func (s *Storage) hasRecentObject(ctx context.Context, bucket bucketClient, prefix string) (bool, error) {
	recent := false
	err := s.eachObject(ctx, bucket, prefix, func(batch []objectInfo) error {
		for _, o := range batch {
			if time.Since(o.Modified) < s.orphanAge {
				recent = true
			}
		}
		return nil
	})
	return recent, err
}

// removeOrphanArtifacts deletes the workspace's artifact objects whose rows
// are gone.
func (s *Storage) removeOrphanArtifacts(ctx context.Context, workspace identity.WorkspaceID) (int, error) {
	prefix := fmt.Sprintf("workspaces/%s/artifacts/", workspace)
	removed := 0
	err := s.eachObject(ctx, s.platform, prefix, func(batch []objectInfo) error {
		ids := make([]uuid.UUID, 0, len(batch))
		byID := map[uuid.UUID]objectInfo{}
		for _, o := range batch {
			if id, err := uuid.Parse(strings.TrimPrefix(o.Key, prefix)); err == nil && time.Since(o.Modified) >= s.orphanAge {
				ids = append(ids, id)
				byID[id] = o
			}
		}
		owned, err := s.queries.ArtifactIDs(ctx, ids)
		if err != nil {
			return fmt.Errorf("read artifact rows: %w", err)
		}
		for _, id := range owned {
			delete(byID, id)
		}
		keys := make([]string, 0, len(byID))
		for _, o := range byID {
			keys = append(keys, o.Key)
		}
		if err := s.deleteKeys(ctx, s.platform, keys); err != nil {
			return err
		}
		removed += len(keys)
		return nil
	})
	return removed, err
}
