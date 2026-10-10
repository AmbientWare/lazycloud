// Package scheduling owns placement: it matches pending containers to hosts
// with free capacity, fairly across workspaces. Execution decides how many
// containers exist; compute supplies hosts and their capacity.
package scheduling

import (
	"cmp"
	"context"
	"fmt"
	"log/slog"
	"slices"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/cpu"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// placementBatch bounds the pending containers one placement transaction
// considers.
const placementBatch = 500

// diskWarm is how long after a disk's release its last host's frame cache
// is taken to still hold the disk.
const diskWarm = time.Hour

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
			traceAssignment(ctx, a)
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
		var maxCPU cpu.Millis
		var maxMemory int64
		for _, h := range hosts {
			maxCPU, maxMemory = max(maxCPU, h.FreeCPUMillis), max(maxMemory, h.FreeMemoryBytes)
		}
		pending, err := q.PendingContainers(ctx, PendingContainersParams{
			MaxFreeCpuMillis: maxCPU, MaxFreeMemoryBytes: maxMemory, Targets: targets(hosts), BatchSize: placementBatch,
			DiskWarmSeconds: diskWarm.Seconds(),
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
		var notify []string
		for _, a := range assigned {
			if a.HostID != nil {
				notify = append(notify, a.HostID.String())
			}
		}
		out.assigned = assigned
		return database.NotifyAll(ctx, tx, database.ChannelHost, notify)
	})
	if err != nil {
		return placeBatchResult{}, fmt.Errorf("placement batch: %w", err)
	}
	return out, nil
}

// targets are the placement targets the hosts serve, in the form
// PendingContainers matches: platform, connection:<id> and
// machine:<workspace>:<name>.
func targets(hosts []compute.HostCapacity) []string {
	seen := map[string]bool{}
	var out []string
	add := func(t string) {
		if !seen[t] {
			seen[t] = true
			out = append(out, t)
		}
	}
	for _, h := range hosts {
		switch h.Kind {
		case compute.KindPlatform:
			add("platform")
		case compute.KindConnection:
			if h.Connection != nil {
				add("connection:" + h.Connection.String())
			}
		case compute.KindMachine:
			for _, ws := range h.Workspaces {
				add("machine:" + ws.String() + ":" + h.Name)
			}
		}
	}
	return out
}

// requirement is what a pending container needs from its host.
func requirement(c PendingContainersRow) compute.Requirement {
	return compute.Requirement{
		Workspace: c.WorkspaceID, Connection: c.ConnectionID, Machine: c.Machine, Region: c.Region, Zone: c.Zone,
		Preemptible: c.Preemptible, GPUs: c.Gpus, GPUCount: int(c.GpuCount),
		CPUMillis: c.CpuMillis, MemoryBytes: c.MemoryBytes, Disks: int(c.Disks),
	}
}

// pack assigns containers, those that cannot run on Spot first so they
// take on-demand room before Spot-tolerant work borrows it, each to the
// host compute.ChooseHost picks, keeping the platform's on-demand warm
// floor free for them. A container whose disk's last host still has it
// cached goes there when it fits. It returns parallel container and host
// id slices.
func pack(hosts []compute.HostCapacity, pending []PendingContainersRow) (ids, hostIDs []uuid.UUID) {
	free := slices.Clone(hosts)
	order := slices.Clone(pending)
	slices.SortStableFunc(order, func(a, b PendingContainersRow) int {
		return cmp.Compare(boolRank(a.Preemptible), boolRank(b.Preemptible))
	})
	floor := compute.DefaultPolicy().OnDemand.Warm.Floor
	for _, c := range order {
		need := requirement(c)
		best := slices.IndexFunc(free, func(h compute.HostCapacity) bool {
			return c.DiskHost != nil && uuid.UUID(h.Host) == *c.DiskHost && h.Fits(need)
		})
		if best < 0 {
			best = compute.ChooseHost(free, need, floor)
		}
		if best < 0 {
			continue
		}
		free[best].Reserve(need)
		ids = append(ids, c.ID)
		hostIDs = append(hostIDs, uuid.UUID(free[best].Host))
	}
	return ids, hostIDs
}

// traceAssignment records the container's wait for placement, from its
// creation to its assignment, in the container's trace.
func traceAssignment(ctx context.Context, a AssignContainersRow) {
	if a.Traceparent == nil {
		return
	}
	attrs := []attribute.KeyValue{telemetry.Container(a.ID.String())}
	if a.HostID != nil {
		attrs = append(attrs, telemetry.Host(a.HostID.String()))
	}
	_, span := telemetry.StartFor(ctx, *a.Traceparent, "scheduling.placement",
		trace.WithTimestamp(a.CreatedAt), trace.WithAttributes(attrs...))
	span.End()
}

// boolRank orders false before true.
func boolRank(b bool) int {
	if b {
		return 1
	}
	return 0
}
