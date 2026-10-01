package execution

import (
	"context"
	"encoding/json"
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
//   - Otherwise an active pod runs one container while it was woken in the
//     last 15 minutes or a container of it is still warm, none while
//     parked, and one always when keep_warm is -1.
//   - A replaced release keeps its containers until the active release has
//     a ready one.
//
// Containers above the count drain, idle ones first.
func (e *Execution) PlanPods(ctx context.Context, logger *slog.Logger) (PlanResult, error) {
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
			rows, err := q.PodReleases(ctx, PodReleasesParams{AfterID: after, BatchSize: planningBatch, StartFailureLimit: startFailureLimit})
			if err != nil {
				return fmt.Errorf("list pod releases: %w", err)
			}
			releases = len(rows)
			for _, row := range rows {
				last = row.ReleaseID
				plan, err := e.planPodIsolated(ctx, tx, row)
				if err != nil {
					if ctx.Err() != nil {
						return ctx.Err()
					}
					logger.ErrorContext(ctx, "plan pod release", "release_id", row.ReleaseID, "error", err)
					continue
				}
				plans = append(plans, plan)
			}
			return nil
		})
		if err != nil {
			return result, fmt.Errorf("pod planning batch: %w", err)
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

func (e *Execution) planPodIsolated(ctx context.Context, tx pgx.Tx, row PodReleasesRow) (releasePlan, error) {
	savepoint, err := tx.Begin(ctx)
	if err != nil {
		return releasePlan{}, fmt.Errorf("begin savepoint: %w", err)
	}
	plan := releasePlan{release: PlanningReleasesRow{ReleaseID: row.ReleaseID}}
	if err := e.planPod(ctx, savepoint, row, &plan); err != nil {
		return releasePlan{}, errors.Join(err, savepoint.Rollback(ctx))
	}
	if err := savepoint.Commit(ctx); err != nil {
		return releasePlan{}, fmt.Errorf("release savepoint: %w", err)
	}
	return plan, nil
}

func (e *Execution) planPod(ctx context.Context, tx pgx.Tx, row PodReleasesRow, plan *releasePlan) error {
	if row.CpuMillis <= 0 || row.MemoryBytes <= 0 {
		return errors.New("release spec has no resources")
	}
	q := e.queries.WithTx(tx)
	active := int(row.Pending + row.Starting + row.Ready)
	desired := podDesired(row, active)
	switch {
	case active < desired:
		count := desired - active
		grant, err := billing.Admit(ctx, tx, billing.Request{
			Workspace: row.WorkspaceID, Start: count, GPUs: int(row.GpuCount),
			GPUModels: billingModels(row.GpuModels), Pinned: row.Pinned,
		})
		var refused *billing.PaymentRequiredError
		if errors.As(err, &refused) {
			// Connections wait and time out until the account can pay.
			return nil
		}
		if err != nil {
			return fmt.Errorf("admit containers: %w", err)
		}
		if count = grant.Start; count == 0 {
			return nil
		}
		var keepWarm *int32
		if !row.AlwaysOn {
			keepWarm = ptr(row.KeepWarmSeconds)
		}
		created, err := q.CreatePendingPodContainers(ctx, CreatePendingPodContainersParams{
			WorkspaceID: row.WorkspaceID, ReleaseID: row.ReleaseID,
			CpuMillis: row.CpuMillis, MemoryBytes: row.MemoryBytes, GpuCount: row.GpuCount,
			RateClass:       string(billing.RateClassFor(row.Pinned, row.Preemptible)),
			KeepWarmSeconds: keepWarm, BlockNetwork: row.BlockNetwork, AllowList: row.AllowList,
			Count: int32(count), //nolint:gosec // Bounded by the pod's count.
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
		drained, err := q.DrainPodContainers(ctx, DrainPodContainersParams{
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
	case row.AlwaysOn:
		want = 1
	case row.Parked:
		return 0
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

// Pod is a pod deployment as a scale, wake or park sees it.
type Pod struct {
	Workload uuid.UUID
	Name     string
	Spec     apitypes.WorkloadSpec
}

func (e *Execution) lockPod(ctx context.Context, q *Queries, workspace identity.WorkspaceID, workload uuid.UUID) (LockPodWorkloadRow, apitypes.WorkloadSpec, error) {
	row, err := q.LockPodWorkload(ctx, LockPodWorkloadParams{ID: workload, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return row, apitypes.WorkloadSpec{}, ErrNotFound
	}
	if err != nil {
		return row, apitypes.WorkloadSpec{}, fmt.Errorf("lock pod: %w", err)
	}
	var spec apitypes.WorkloadSpec
	if row.Spec != nil {
		if err := json.Unmarshal(row.Spec, &spec); err != nil {
			return row, spec, fmt.Errorf("decode release spec: %w", err)
		}
	}
	return row, spec, nil
}

// ScalePod holds an active pod at containers.
func (e *Execution) ScalePod(ctx context.Context, workspace identity.WorkspaceID, workload uuid.UUID, containers int) error {
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		row, spec, err := e.lockPod(ctx, q, workspace, workload)
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
		case spec.Disks != nil && len(*spec.Disks) > 0 && containers > 1:
			return &InvalidError{Reason: "a pod with a disk runs one container; scale it to 0 or 1"}
		case containers == 0 && spec.KeepWarmSeconds != nil && *spec.KeepWarmSeconds == -1:
			return &InvalidError{Reason: "always-on pod deployments cannot be scaled to zero"}
		}
		if err := q.SetPodReplicas(ctx, SetPodReplicasParams{WorkloadID: workload, Replicas: ptr(int32(containers))}); err != nil { //nolint:gosec // The schema bounds containers.
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
// devbox start does.
func (e *Execution) WakePod(ctx context.Context, workspace identity.WorkspaceID, workload uuid.UUID) error {
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		row, _, err := e.lockPod(ctx, q, workspace, workload)
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
		return database.Notify(ctx, tx, database.ChannelExecution, row.ActiveReleaseID.String())
	})
	if err != nil {
		return fmt.Errorf("wake pod: %w", err)
	}
	return nil
}

// ParkPod stops a pod's serve containers and keeps them stopped until the
// next wake, as a devbox stop does. Its disks are saved as they stop.
func (e *Execution) ParkPod(ctx context.Context, workspace identity.WorkspaceID, workload uuid.UUID) error {
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		if _, _, err := e.lockPod(ctx, q, workspace, workload); err != nil {
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
}

// ReadyPodContainers lists the pod's ready serve containers, the active
// release's first.
func (e *Execution) ReadyPodContainers(ctx context.Context, workload uuid.UUID) ([]PodContainer, error) {
	rows, err := e.queries.ReadyPodContainers(ctx, workload)
	if err != nil {
		return nil, fmt.Errorf("list pod containers: %w", err)
	}
	out := make([]PodContainer, 0, len(rows))
	for _, r := range rows {
		if r.HostID != nil {
			out = append(out, PodContainer{Container: ContainerID(r.ID), Host: *r.HostID, Release: r.ReleaseID})
		}
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
