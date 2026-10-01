package execution

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
)

// HTTP workloads and previews get containers from traffic and leases rather
// than queued tasks. Their containers never run attempts: requests reach
// them over the edge's data connection, and no task row is written per
// request.

// EndpointLoad is one edge's demand on one release: requests in flight,
// requests waiting for a container, and the largest demand within the
// release's keep-warm window.
type EndpointLoad struct {
	Release    uuid.UUID
	InFlight   int
	Waiting    int
	WindowPeak int
}

// EndpointLoadTTL is how long an edge's published demand counts without a
// renewal.
const EndpointLoadTTL = 15 * time.Second

// PublishEndpointLoads records edge's demand on each release in one
// statement. Planning wakes for wake, the releases that just gained waiting
// requests, so a cold start does not wait for the next pass.
func (e *Execution) PublishEndpointLoads(ctx context.Context, edge uuid.UUID, loads []EndpointLoad, wake []uuid.UUID) error {
	if len(loads) == 0 {
		return nil
	}
	params := UpsertEndpointLoadsParams{
		ReleaseIds: make([]uuid.UUID, len(loads)),
		EdgeID:     edge,
		InFlight:   make([]int32, len(loads)),
		Waiting:    make([]int32, len(loads)),
		WindowPeak: make([]int32, len(loads)),
		TtlSeconds: EndpointLoadTTL.Seconds(),
	}
	for n, l := range loads {
		params.ReleaseIds[n] = l.Release
		params.InFlight[n] = clampInt32(l.InFlight)
		params.Waiting[n] = clampInt32(l.Waiting)
		params.WindowPeak[n] = clampInt32(l.WindowPeak)
	}
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		if err := e.queries.WithTx(tx).UpsertEndpointLoads(ctx, params); err != nil {
			return fmt.Errorf("upsert endpoint loads: %w", err)
		}
		payloads := make([]string, len(wake))
		for n, release := range wake {
			payloads[n] = release.String()
		}
		return notifyAll(ctx, tx, database.ChannelExecution, payloads)
	})
	if err != nil {
		return fmt.Errorf("publish endpoint loads: %w", err)
	}
	return nil
}

// EndpointContainer is a ready container of an HTTP workload.
type EndpointContainer struct {
	Release   uuid.UUID
	Version   int
	Container ContainerID
	Host      uuid.UUID
}

// EndpointContainers lists the ready containers of every release of
// workload, newest version first.
func (e *Execution) EndpointContainers(ctx context.Context, workload uuid.UUID) ([]EndpointContainer, error) {
	rows, err := e.queries.EndpointContainers(ctx, workload)
	if err != nil {
		return nil, fmt.Errorf("list endpoint containers: %w", err)
	}
	out := make([]EndpointContainer, 0, len(rows))
	for _, row := range rows {
		if row.HostID == nil || row.Version == nil {
			continue
		}
		out = append(out, EndpointContainer{Release: row.ReleaseID, Version: int(*row.Version), Container: ContainerID(row.ContainerID), Host: *row.HostID})
	}
	return out, nil
}

// ReleaseFailure explains why containers of a release cannot start.
type ReleaseFailure struct {
	// LoadError is the handler's import error, when that is the reason.
	LoadError string
	// StartFailures counts consecutive failed preparations.
	StartFailures int
}

// ReleaseFailures returns the releases among ids whose containers cannot
// start.
func (e *Execution) ReleaseFailures(ctx context.Context, ids []uuid.UUID) (map[uuid.UUID]ReleaseFailure, error) {
	rows, err := e.queries.ReleaseFailures(ctx, ReleaseFailuresParams{Ids: ids, StartFailureLimit: startFailureLimit})
	if err != nil {
		return nil, fmt.Errorf("read release failures: %w", err)
	}
	out := make(map[uuid.UUID]ReleaseFailure, len(rows))
	for _, row := range rows {
		out[row.ID] = ReleaseFailure{LoadError: row.LoadError, StartFailures: int(row.StartFailures)}
	}
	return out, nil
}

// PlanServing decides how many containers each HTTP release and preview
// needs, under the same planning lock as Plan, so max_containers holds
// across scheduler replicas.
//
//   - A preview keeps one container while its lease lives.
//   - The active release of an HTTP workload gets clamp(ceil(max(demand,
//     window peak) / tasks_per_container), min, max), where demand and the
//     keep-warm window peak come from the edges' unexpired leases.
//   - A replaced release keeps its containers until the active release has a
//     ready one, so a deploy never drops traffic, then follows its own pinned
//     demand.
//   - A release whose handler failed to load gets none until it is replaced.
func (e *Execution) PlanServing(ctx context.Context, logger *slog.Logger) (PlanResult, error) {
	var result PlanResult
	after := uuid.Nil
	for {
		var plans []releasePlan
		var releases int
		var last uuid.UUID
		skipped := false
		err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
			plans, releases, skipped = nil, 0, false
			q := e.queries.WithTx(tx)
			locked, err := q.TryPlanningLock(ctx)
			if err != nil {
				return fmt.Errorf("try planning lock: %w", err)
			}
			if !locked {
				skipped = true
				return nil
			}
			rows, err := q.ServingReleases(ctx, ServingReleasesParams{AfterID: after, BatchSize: planningBatch, StartFailureLimit: startFailureLimit})
			if err != nil {
				return fmt.Errorf("list serving releases: %w", err)
			}
			releases = len(rows)
			for _, row := range rows {
				last = row.ReleaseID
				plan, err := e.planServingIsolated(ctx, tx, row)
				if err != nil {
					if ctx.Err() != nil {
						return ctx.Err()
					}
					logger.ErrorContext(ctx, "plan serving release", "release_id", row.ReleaseID, "error", err)
					continue
				}
				plans = append(plans, plan)
			}
			return q.DeleteExpiredEndpointLoads(ctx)
		})
		if err != nil {
			return result, fmt.Errorf("serving planning batch: %w", err)
		}
		if skipped {
			result.Skipped = true
			return result, nil
		}
		for _, plan := range plans {
			result.add(plan)
			logPlan(ctx, logger, plan)
		}
		if releases < planningBatch {
			return result, nil
		}
		after = last
	}
}

func (e *Execution) planServingIsolated(ctx context.Context, tx pgx.Tx, row ServingReleasesRow) (releasePlan, error) {
	savepoint, err := tx.Begin(ctx)
	if err != nil {
		return releasePlan{}, fmt.Errorf("begin savepoint: %w", err)
	}
	plan := releasePlan{release: PlanningReleasesRow{ReleaseID: row.ReleaseID}}
	if err := e.planServing(ctx, savepoint, row, &plan); err != nil {
		return releasePlan{}, errors.Join(err, savepoint.Rollback(ctx))
	}
	if err := savepoint.Commit(ctx); err != nil {
		return releasePlan{}, fmt.Errorf("release savepoint: %w", err)
	}
	return plan, nil
}

func (e *Execution) planServing(ctx context.Context, tx pgx.Tx, row ServingReleasesRow, plan *releasePlan) error {
	if row.CpuMillis <= 0 || row.MemoryBytes <= 0 {
		return errors.New("release spec has no resources")
	}
	q := e.queries.WithTx(tx)
	active := int(row.Pending + row.Starting + row.Ready)
	live := active + int(row.Draining)
	desired := servingDesired(row, active)
	switch {
	case active < desired:
		count := min(desired-active, int(row.MaxContainers)-live)
		if row.Preview {
			count = min(desired-active, 1-live)
		}
		if count <= 0 {
			return nil
		}
		models := make([]billing.GPUType, len(row.GpuModels))
		for n, m := range row.GpuModels {
			models[n] = billing.GPUType(m)
		}
		grant, err := billing.Admit(ctx, tx, billing.Request{
			Workspace: row.WorkspaceID, Start: count, GPUs: int(row.GpuCount), GPUModels: models, Pinned: row.Pinned,
		})
		var refused *billing.PaymentRequiredError
		if errors.As(err, &refused) {
			// The account cannot pay; requests wait and time out until it
			// can.
			return nil
		}
		if err != nil {
			return fmt.Errorf("admit containers: %w", err)
		}
		if count = grant.Start; count == 0 {
			return nil
		}
		created, err := q.CreatePendingContainers(ctx, CreatePendingContainersParams{
			WorkspaceID: row.WorkspaceID,
			ReleaseID:   row.ReleaseID,
			Slots:       row.Slots,
			CpuMillis:   row.CpuMillis,
			MemoryBytes: row.MemoryBytes,
			GpuCount:    row.GpuCount,
			RateClass:   string(billing.RateClassFor(row.Pinned, row.Preemptible)),
			Count:       int32(count), //nolint:gosec // Bounded by max_containers.
		})
		if err != nil {
			return fmt.Errorf("create containers: %w", err)
		}
		plan.created = created
	case active > desired:
		excess := active - desired
		if pending := min(excess, int(row.Pending)); pending > 0 {
			stopped, err := q.StopPendingContainers(ctx, StopPendingContainersParams{
				ReleaseID: row.ReleaseID, Count: int32(pending), //nolint:gosec // Bounded by live containers.
			})
			if err != nil {
				return fmt.Errorf("stop pending containers: %w", err)
			}
			plan.stopped = stopped
			excess -= len(stopped)
		}
		if excess <= 0 {
			return nil
		}
		drained, err := q.DrainNewestContainers(ctx, DrainNewestContainersParams{
			ReleaseID: row.ReleaseID, Count: int32(excess), //nolint:gosec // Bounded by live containers.
		})
		if err != nil {
			return fmt.Errorf("drain containers: %w", err)
		}
		for _, d := range drained {
			plan.drained = append(plan.drained, drainedContainer{id: d.ID, host: d.HostID})
		}
		return notifyHosts(ctx, tx, plan.drained)
	}
	return nil
}

// servingDesired is how many active containers the release should have,
// given active now.
func servingDesired(row ServingReleasesRow, active int) int {
	switch {
	case row.Preview:
		if row.PreviewLive && !row.Failed {
			return 1
		}
		return 0
	case row.Stopping || row.Failed:
		return 0
	}
	demand := max(int(row.Demand), int(row.Peak))
	desired := desiredContainers(demand, int(row.TasksPerContainer), 0, int(row.MaxContainers))
	if row.Active {
		return desiredContainers(demand, int(row.TasksPerContainer), int(row.MinContainers), int(row.MaxContainers))
	}
	if !row.ActiveReady {
		// The edge still sends this release the traffic the new one will
		// take over once it is ready.
		return max(desired, active)
	}
	return desired
}

func clampInt32(n int) int32 {
	return int32(min(max(n, 0), 1<<31-1)) //nolint:gosec // clamped above
}

// AdmitCold asks billing whether the workspace may start a container of
// spec for a request that has none, so the edge can refuse the request at
// once instead of letting it wait for a container planning will not start.
// It returns billing's PaymentRequiredError or LimitError.
func (e *Execution) AdmitCold(ctx context.Context, workspace uuid.UUID, spec apitypes.FunctionSpec) error {
	req := billing.Request{Workspace: workspace, Cold: true}
	if r := spec.Resources; r.GpuCount != nil && *r.GpuCount > 0 {
		req.GPUs = *r.GpuCount
	} else if r.Gpu != nil && len(*r.Gpu) > 0 {
		req.GPUs = 1
	}
	if r := spec.Resources; r.Gpu != nil {
		for _, m := range *r.Gpu {
			req.GPUModels = append(req.GPUModels, billing.GPUType(m))
		}
	}
	if p := spec.Placement; p != nil {
		req.Pinned = (p.Region != nil && *p.Region != "") || (p.AvailabilityZone != nil && *p.AvailabilityZone != "")
	}
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		_, err := billing.Admit(ctx, tx, req)
		return err //nolint:wrapcheck // billing's refusal passes through typed
	})
	if err != nil {
		return fmt.Errorf("admit a cold request: %w", err)
	}
	return nil
}
