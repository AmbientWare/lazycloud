package execution

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
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
		return database.NotifyAll(ctx, tx, database.ChannelExecution, payloads)
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
	// Traceparent is the trace of the container's start.
	Traceparent string
}

// EndpointContainers lists the ready containers of every release of
// workload, newest version first.
func (e *Execution) EndpointContainers(ctx context.Context, workload uuid.UUID) ([]EndpointContainer, error) {
	rows, err := e.queries.ReadyContainers(ctx, workload)
	if err != nil {
		return nil, fmt.Errorf("list endpoint containers: %w", err)
	}
	out := make([]EndpointContainer, 0, len(rows))
	for _, row := range rows {
		if row.Version != nil {
			out = append(out, EndpointContainer{
				Release: row.ReleaseID, Version: int(*row.Version), Container: ContainerID(row.ID), Host: *row.HostID,
				Traceparent: deref(row.Traceparent),
			})
		}
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
//   - A release whose handler failed to load or that reached the start
//     failure limit gets none until it is replaced or RetryStarts retries it.
func (e *Execution) PlanServing(ctx context.Context, logger *slog.Logger) (PlanResult, error) {
	return planReleases(ctx, e, logger,
		func(ctx context.Context, q *Queries, after uuid.UUID) ([]ServingReleasesRow, error) {
			return q.ServingReleases(ctx, ServingReleasesParams{AfterID: after, BatchSize: planningBatch, StartFailureLimit: startFailureLimit})
		},
		func(r ServingReleasesRow) ReleaseShape { return r.ReleaseShape }, e.planServing)
}

func (e *Execution) planServing(ctx context.Context, tx pgx.Tx, row ServingReleasesRow, plan *releasePlan) error {
	shape := row.ReleaseShape
	if shape.CpuMillis <= 0 || shape.MemoryBytes <= 0 {
		return errors.New("release spec has no resources")
	}
	leave, err := e.leaving(ctx, tx, plan, row.Leaving)
	if err != nil {
		return err
	}
	active, live := leave.counts(row.Pending, row.Starting, row.Ready, row.Draining)
	desired := servingDesired(row, active)
	room := int(row.MaxContainers) - live
	if row.Preview {
		room = 1 - live
	}
	pairs, grow := leave.starts(active, desired, room)
	if pairs+grow > 0 {
		return e.start(ctx, tx, plan, row.WorkspaceID, shape, shape.create(row.WorkspaceID, row.Slots),
			leave.unpaired[:pairs], pairs+grow, live == 0 && row.Demand == 0 && row.Peak == 0)
	}
	return e.trim(ctx, tx, plan, active-desired, row.Pending, e.drainExcess(ctx, tx, plan))
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
// It returns billing's GPUUnavailableError, PaymentRequiredError or
// LimitError.
func (e *Execution) AdmitCold(ctx context.Context, workspace uuid.UUID, spec apitypes.WorkloadSpec) error {
	req := billing.DeclaredBy(&spec.Resources, spec.Placement, spec.Autoscaler).Request(workspace)
	req.Cold = true
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		_, err := billing.Admit(ctx, tx, req)
		return err //nolint:wrapcheck // billing's refusal passes through typed
	})
	if err != nil {
		return fmt.Errorf("admit a cold request: %w", err)
	}
	return nil
}
