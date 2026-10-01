package storage

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

const (
	// sweepBatch bounds the items one sweep step claims.
	sweepBatch = 100
	// measureEvery is how often a volume's stored bytes are recounted.
	measureEvery = 10 * time.Minute
)

// SweepResult counts what one sweep removed or measured.
type SweepResult struct {
	Artifacts, Volumes, Disks, MapEntries, Grants, Measured int
}

// Sweep runs one bounded pass of storage retention and cleanup: expired and
// abandoned artifacts, deleted volumes and disks, expired map entries,
// expired provider keys and stale volume sizes. Each step claims its items
// with SKIP LOCKED, so several schedulers share the work; one item's failure
// is logged and leaves it for the next pass without blocking the rest.
func (s *Storage) Sweep(ctx context.Context, logger *slog.Logger) (SweepResult, error) {
	var errs []error
	step := func(name string, n int, err error) int {
		if err != nil {
			errs = append(errs, fmt.Errorf("%s: %w", name, err))
		}
		return n
	}
	var out SweepResult
	n, err := s.sweepArtifacts(ctx)
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
	return out, errors.Join(errs...)
}

// RunSweeper sweeps every interval until ctx ends.
func (s *Storage) RunSweeper(ctx context.Context, interval time.Duration, logger *slog.Logger) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		if _, err := s.Sweep(ctx, logger); err != nil && ctx.Err() == nil {
			logger.WarnContext(ctx, "storage sweep incomplete", "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

// sweepArtifacts deletes expired artifacts and uploads abandoned for a day.
// Rows are deleted in the transaction that claimed them, after their bytes.
func (s *Storage) sweepArtifacts(ctx context.Context) (int, error) {
	var n int
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		q := s.queries.WithTx(tx)
		rows, err := q.ExpiredArtifacts(ctx, sweepBatch)
		if err != nil {
			return fmt.Errorf("claim expired artifacts: %w", err)
		}
		keys := make([]string, len(rows))
		ids := make([]uuid.UUID, len(rows))
		for i, row := range rows {
			ids[i] = row.ID
			keys[i] = artifactKey(identity.WorkspaceID(row.WorkspaceID), row.ID)
			if row.UploadID != nil {
				if err := s.abortMultipart(ctx, s.bucket, keys[i], *row.UploadID); err != nil {
					return err
				}
			}
		}
		if err := s.deleteKeys(ctx, s.bucket, keys); err != nil {
			return err
		}
		n = len(rows)
		return q.DeleteArtifactRows(ctx, ids)
	})
	return n, inTx(err)
}

// sweepVolumes removes deleted volumes' files, then their rows. A volume
// whose files cannot be removed now stays for the next pass.
func (s *Storage) sweepVolumes(ctx context.Context, logger *slog.Logger) (int, error) {
	var n int
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		q := s.queries.WithTx(tx)
		rows, err := q.DeletingVolumes(ctx, sweepBatch)
		if err != nil {
			return fmt.Errorf("claim deleted volumes: %w", err)
		}
		for _, row := range rows {
			if row.Bucket != "" {
				if err := s.deletePrefix(ctx, row.Bucket, volumePrefix(row.ID)); err != nil {
					logger.WarnContext(ctx, "removing deleted volume's files failed", "volume", row.ID.String(), "error", err)
					continue
				}
			}
			if err := q.DeleteVolumeRow(ctx, row.ID); err != nil {
				return fmt.Errorf("delete volume row: %w", err)
			}
			n++
		}
		return nil
	})
	return n, inTx(err)
}

func (s *Storage) sweepDisks(ctx context.Context, logger *slog.Logger) (int, error) {
	var n int
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		q := s.queries.WithTx(tx)
		rows, err := q.DeletingDisks(ctx, sweepBatch)
		if err != nil {
			return fmt.Errorf("claim deleted disks: %w", err)
		}
		for _, row := range rows {
			if row.Bucket != "" {
				if err := s.deletePrefix(ctx, row.Bucket, "disks/"+row.ID.String()+"/"); err != nil {
					logger.WarnContext(ctx, "removing deleted disk's objects failed", "disk", row.ID.String(), "error", err)
					continue
				}
			}
			if err := q.DeleteDiskRow(ctx, row.ID); err != nil {
				return fmt.Errorf("delete disk row: %w", err)
			}
			n++
		}
		return nil
	})
	return n, inTx(err)
}

// sweepGrants deletes expired provider keys.
func (s *Storage) sweepGrants(ctx context.Context, logger *slog.Logger) (int, error) {
	if s.buckets == nil {
		return 0, nil
	}
	var n int
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		q := s.queries.WithTx(tx)
		keys, err := q.ExpiredStorageGrants(ctx, sweepBatch)
		if err != nil {
			return fmt.Errorf("claim expired grants: %w", err)
		}
		for _, key := range keys {
			if err := s.buckets.revoke(ctx, key); err != nil {
				logger.WarnContext(ctx, "revoking expired storage key failed", "error", err)
				continue
			}
			if err := q.DeleteStorageGrant(ctx, key); err != nil {
				return fmt.Errorf("delete grant row: %w", err)
			}
			n++
		}
		return nil
	})
	return n, inTx(err)
}

// measureVolumes recounts the stored bytes of volumes not measured within
// measureEvery.
func (s *Storage) measureVolumes(ctx context.Context, logger *slog.Logger) (int, error) {
	var n int
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		q := s.queries.WithTx(tx)
		rows, err := q.VolumesToMeasure(ctx, VolumesToMeasureParams{EverySeconds: measureEvery.Seconds(), MaxRows: sweepBatch / 10})
		if err != nil {
			return fmt.Errorf("claim volumes to measure: %w", err)
		}
		for _, row := range rows {
			var size int64
			err := s.eachObject(ctx, row.Bucket, volumePrefix(row.ID), func(batch []objectInfo) error {
				for _, o := range batch {
					size += o.Size
				}
				return nil
			})
			if err != nil {
				logger.WarnContext(ctx, "measuring volume failed", "volume", row.ID.String(), "error", err)
				continue
			}
			if err := q.RecordVolumeSize(ctx, RecordVolumeSizeParams{ID: row.ID, SizeBytes: size}); err != nil {
				return fmt.Errorf("record volume size: %w", err)
			}
			n++
		}
		return nil
	})
	return n, inTx(err)
}

// inTx wraps the error of a sweep transaction.
func inTx(err error) error {
	if err != nil {
		return fmt.Errorf("sweep transaction: %w", err)
	}
	return nil
}
