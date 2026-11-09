package execution

import (
	"context"
	"errors"
	"fmt"
	"log/slog"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// PlanPods decides how many serve containers each pod release needs, under
// the planning lock, so counts hold across scheduler replicas:
//
//   - A scaled pod holds its count.
//   - Otherwise an active pod runs none while parked, one always when
//     keep_warm is -1, and one while it was woken in the last 15 minutes or
//     a container of it is still warm.
//   - A replaced release keeps its containers until the active release has
//     a ready one.
//
// Containers above the count drain, idle ones first.
func (e *Execution) PlanPods(ctx context.Context, logger *slog.Logger) (PlanResult, error) {
	return planReleases(ctx, e, logger,
		func(ctx context.Context, q *Queries, after uuid.UUID) ([]PodReleasesRow, error) {
			return q.PodReleases(ctx, PodReleasesParams{AfterID: after, BatchSize: planningBatch, StartFailureLimit: startFailureLimit})
		},
		func(r PodReleasesRow) ReleaseShape { return r.ReleaseShape }, e.planPod)
}

func (e *Execution) planPod(ctx context.Context, tx pgx.Tx, row PodReleasesRow, plan *releasePlan) error {
	shape := row.ReleaseShape
	if shape.CpuMillis <= 0 || shape.MemoryBytes <= 0 {
		return errors.New("release spec has no resources")
	}
	active := int(row.Pending + row.Starting + row.Ready)
	desired := podDesired(row, active)
	if active < desired {
		create := shape.create(row.WorkspaceID, 1)
		if !row.AlwaysOn {
			create.KeepWarmSeconds = new(row.KeepWarmSeconds)
		}
		create.BlockNetwork, create.AllowList = row.BlockNetwork, row.AllowList
		return e.start(ctx, tx, plan, shape, create, nil, desired-active, active+int(row.Draining) == 0)
	}
	return e.trim(ctx, tx, plan, active-desired, row.Pending, e.drainExcess(ctx, tx, plan))
}

// podDesired is how many active serve containers the pod release should
// have, given active now.
func podDesired(row PodReleasesRow, active int) int {
	if row.Stopping || row.Failed {
		return 0
	}
	limit := int(row.MaxContainers)
	if row.HasDisks {
		limit = 1
	}
	if !row.Active {
		if !row.ActiveReady {
			// Connections still reach this release until the new one is
			// ready.
			return active
		}
		return 0
	}
	if row.Replicas != nil {
		return int(*row.Replicas)
	}
	want := 0
	switch {
	case row.Parked:
		return 0
	case row.AlwaysOn:
		want = 1
	case row.Woken:
		want = 1
	}
	return min(max(want, int(row.Warm)), max(limit, 1))
}

func billingModels(models []string) []billing.GPUType {
	out := make([]billing.GPUType, len(models))
	for n, m := range models {
		out[n] = billing.GPUType(m)
	}
	return out
}

func (e *Execution) lockPod(ctx context.Context, q *Queries, workspace identity.WorkspaceID, workload uuid.UUID) (LockPodWorkloadRow, error) {
	row, err := q.LockPodWorkload(ctx, LockPodWorkloadParams{ID: workload, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return row, ErrNotFound
	}
	if err != nil {
		return row, fmt.Errorf("lock pod: %w", err)
	}
	return row, nil
}

// ScalePod holds an active pod at containers.
func (e *Execution) ScalePod(ctx context.Context, workspace identity.WorkspaceID, workload uuid.UUID, containers int) error {
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		row, err := e.lockPod(ctx, q, workspace, workload)
		if err != nil {
			return err
		}
		switch {
		case row.Kind != string(apitypes.WorkloadKindPod):
			return &InvalidError{Reason: "only pods scale; functions and endpoints follow their autoscaler"}
		case row.AppState != "active":
			return &ConflictError{Reason: "the app is paused"}
		case row.DesiredState != "active":
			return &ConflictError{Reason: "cannot scale an inactive deployment"}
		case row.HasDisks && containers > 1:
			return &InvalidError{Reason: "a pod with a disk runs one container; scale it to 0 or 1"}
		case containers == 0 && row.AlwaysOn:
			return &InvalidError{Reason: "always-on pod deployments cannot be scaled to zero"}
		}
		if err := q.SetPodReplicas(ctx, SetPodReplicasParams{WorkloadID: workload, Replicas: new(int32(containers))}); err != nil { //nolint:gosec // The schema bounds containers.
			return fmt.Errorf("set replicas: %w", err)
		}
		if row.ActiveReleaseID == nil {
			return nil
		}
		return database.Notify(ctx, tx, database.ChannelExecution, row.ActiveReleaseID.String())
	})
	if err != nil {
		return fmt.Errorf("scale pod: %w", err)
	}
	return nil
}

// WakePod asks for a container of an active pod now, as a connection or a
// devbox start does, and retries its release if it stopped starting. It
// returns the Failure RetryStarts holds the release on, or nil.
func (e *Execution) WakePod(ctx context.Context, workspace identity.WorkspaceID, workload uuid.UUID) (*Failure, error) {
	var held *Failure
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		row, err := e.lockPod(ctx, q, workspace, workload)
		if err != nil {
			return err
		}
		if row.AppState != "active" || row.DesiredState != "active" {
			return &ConflictError{Reason: "the pod is stopped; deploy it again to connect"}
		}
		if err := q.WakePod(ctx, workload); err != nil {
			return fmt.Errorf("wake pod: %w", err)
		}
		if row.ActiveReleaseID == nil {
			return nil
		}
		release := *row.ActiveReleaseID
		retries, err := RetryStarts(ctx, tx, []uuid.UUID{release})
		if err != nil {
			return err
		}
		held = retries[release]
		return database.Notify(ctx, tx, database.ChannelExecution, release.String())
	})
	if err != nil {
		return nil, fmt.Errorf("wake pod: %w", err)
	}
	return held, nil
}

// ParkPod stops a pod's serve containers, starting ones included, and keeps
// them stopped until the next wake or scale, as a devbox stop does. Its disks
// are saved as they stop.
func (e *Execution) ParkPod(ctx context.Context, workspace identity.WorkspaceID, workload uuid.UUID) error {
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		if _, err := e.lockPod(ctx, q, workspace, workload); err != nil {
			return err
		}
		if err := q.ParkPod(ctx, workload); err != nil {
			return fmt.Errorf("park pod: %w", err)
		}
		if _, err := q.StopPendingServeContainersOfWorkload(ctx, workload); err != nil {
			return fmt.Errorf("stop pending containers: %w", err)
		}
		rows, err := q.DrainServeContainersOfWorkload(ctx, workload)
		if err != nil {
			return fmt.Errorf("drain containers: %w", err)
		}
		drained := make([]drainedContainer, len(rows))
		for n, r := range rows {
			drained[n] = drainedContainer{id: r.ID, host: r.HostID}
		}
		return notifyHosts(ctx, tx, drained)
	})
	if err != nil {
		return fmt.Errorf("park pod: %w", err)
	}
	return nil
}

// PodContainer is a ready serve container of a pod.
type PodContainer struct {
	Container ContainerID
	Host      uuid.UUID
	Release   uuid.UUID
	// Traceparent is the trace of the container's start.
	Traceparent string
}

// ReadyPodContainers lists the pod's ready serve containers, the active
// release's first.
func (e *Execution) ReadyPodContainers(ctx context.Context, workload uuid.UUID) ([]PodContainer, error) {
	rows, err := e.queries.ReadyContainers(ctx, workload)
	if err != nil {
		return nil, fmt.Errorf("list pod containers: %w", err)
	}
	out := make([]PodContainer, len(rows))
	for n, r := range rows {
		out[n] = PodContainer{Container: ContainerID(r.ID), Host: *r.HostID, Release: r.ReleaseID, Traceparent: deref(r.Traceparent)}
	}
	return out, nil
}

// PodReplicas returns the count a scale set, or nil when the pod follows
// its connections.
func (e *Execution) PodReplicas(ctx context.Context, workload uuid.UUID) (*int, error) {
	row, err := e.queries.PodState(ctx, workload)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read pod state: %w", err)
	}
	return intOf(row.Replicas), nil
}
