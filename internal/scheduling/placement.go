// Package scheduling owns placement: it matches pending containers to hosts
// with free capacity, fairly across workspaces. Execution decides how many
// containers exist; compute supplies hosts and their capacity.
package scheduling

import (
	"context"
	"fmt"
	"log/slog"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
)

// placementBatch bounds the pending containers one placement transaction
// considers.
const placementBatch = 500

// Scheduling is the placement owner.
type Scheduling struct {
	pool    *pgxpool.Pool
	queries *Queries
	logger  *slog.Logger
}

// NewScheduling returns the placement owner over pool.
func NewScheduling(pool *pgxpool.Pool, logger *slog.Logger) *Scheduling {
	return &Scheduling{pool: pool, queries: New(pool), logger: logger}
}

// PlacementResult summarizes one placement pass.
type PlacementResult struct {
	// Skipped means another placer held the placement lock.
	Skipped bool
	// Assigned counts containers moved to starting on a host.
	Assigned int
}

// Place assigns pending containers to online hosts until nothing more fits.
// Each batch runs in one transaction under the placement lock: it reads host
// capacity net of every live container, best-fit packs the batch in
// round-robin order and assigns with a conditional update. Containers that fit
// nowhere stay pending until capacity frees up.
func (s *Scheduling) Place(ctx context.Context) (PlacementResult, error) {
	var result PlacementResult
	for {
		batch, err := s.placeBatch(ctx)
		if err != nil {
			return result, err
		}
		if batch.skipped {
			result.Skipped = true
			return result, nil
		}
		result.Assigned += len(batch.assigned)
		for _, a := range batch.assigned {
			s.logger.InfoContext(ctx, "container assigned", "container_id", a.ID, "release_id", a.ReleaseID, "host_id", a.HostID)
		}
		// Another batch helps only if this one was full and made progress.
		if batch.considered < placementBatch || len(batch.assigned) == 0 {
			return result, nil
		}
	}
}

type placeBatchResult struct {
	skipped    bool
	considered int
	assigned   []AssignContainersRow
}

func (s *Scheduling) placeBatch(ctx context.Context) (placeBatchResult, error) {
	var out placeBatchResult
	err := pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
		out = placeBatchResult{}
		q := s.queries.WithTx(tx)
		locked, err := q.TryPlacementLock(ctx)
		if err != nil {
			return fmt.Errorf("try placement lock: %w", err)
		}
		if !locked {
			out.skipped = true
			return nil
		}
		hosts, err := compute.AvailableCapacity(ctx, tx)
		if err != nil {
			return err
		}
		if len(hosts) == 0 {
			return nil
		}
		var maxCPU, maxMemory int64
		for _, h := range hosts {
			maxCPU, maxMemory = max(maxCPU, h.FreeCPUMillis), max(maxMemory, h.FreeMemoryBytes)
		}
		pending, err := q.PendingContainers(ctx, PendingContainersParams{
			MaxFreeCpuMillis: maxCPU, MaxFreeMemoryBytes: maxMemory, BatchSize: placementBatch,
		})
		if err != nil {
			return fmt.Errorf("list pending containers: %w", err)
		}
		out.considered = len(pending)
		ids, hostIDs := pack(hosts, pending)
		if len(ids) == 0 {
			return nil
		}
		assigned, err := q.AssignContainers(ctx, AssignContainersParams{Ids: ids, HostIds: hostIDs})
		if err != nil {
			return fmt.Errorf("assign containers: %w", err)
		}
		notified := map[uuid.UUID]bool{}
		for _, a := range assigned {
			if a.HostID == nil || notified[*a.HostID] {
				continue
			}
			notified[*a.HostID] = true
			if err := database.Notify(ctx, tx, database.ChannelHost, a.HostID.String()); err != nil {
				return err
			}
		}
		out.assigned = assigned
		return nil
	})
	if err != nil {
		return placeBatchResult{}, fmt.Errorf("placement batch: %w", err)
	}
	return out, nil
}

// pack assigns containers in order, each to the host it fits most tightly:
// the host whose free CPU and memory, as fractions of its size, sum lowest
// after placement. Filling tight hosts first keeps large holes for large
// containers. It returns parallel container and host id slices.
func pack(hosts []compute.HostCapacity, pending []PendingContainersRow) (ids, hostIDs []uuid.UUID) {
	free := make([]compute.HostCapacity, len(hosts))
	copy(free, hosts)
	for _, c := range pending {
		best := -1
		var bestScore float64
		for i, h := range free {
			if h.CPUMillis <= 0 || h.MemoryBytes <= 0 || h.FreeCPUMillis < c.CpuMillis || h.FreeMemoryBytes < c.MemoryBytes {
				continue
			}
			score := float64(h.FreeCPUMillis-c.CpuMillis)/float64(h.CPUMillis) +
				float64(h.FreeMemoryBytes-c.MemoryBytes)/float64(h.MemoryBytes)
			if best < 0 || score < bestScore {
				best, bestScore = i, score
			}
		}
		if best < 0 {
			continue
		}
		free[best].FreeCPUMillis -= c.CpuMillis
		free[best].FreeMemoryBytes -= c.MemoryBytes
		ids = append(ids, c.ID)
		hostIDs = append(hostIDs, uuid.UUID(free[best].Host))
	}
	return ids, hostIDs
}
