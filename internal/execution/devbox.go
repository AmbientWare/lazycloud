package execution

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// PodView is a pod deployment and what its newest container is doing, as
// a devbox page and the connect spinner read it.
type PodView struct {
	Workload uuid.UUID
	Name     string
	App      string
	// Ambiguous means another app has a pod of the same name.
	Ambiguous bool
	Spec      apitypes.FunctionSpec
	Active    bool
	Phase     apitypes.DevboxPhase
	Reason    string
	Container *ContainerID
	Failed    *ContainerID
	// Connections are the open connections to the container.
	Connections int
	// IdleDeadline is when the container stops without a connection; nil
	// while connected or always on.
	IdleDeadline *time.Time
}

// PodView reads a pod deployment of the workspace. savingDisk says the
// pod's disk is still held by a stopped container that is saving it.
func (e *Execution) PodView(ctx context.Context, workspace identity.WorkspaceID, workload uuid.UUID, savingDisk func(apitypes.FunctionSpec) (bool, error)) (PodView, error) {
	row, err := e.queries.DevboxWorkload(ctx, DevboxWorkloadParams{ID: workload, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return PodView{}, ErrNotFound
	}
	if err != nil {
		return PodView{}, fmt.Errorf("read pod: %w", err)
	}
	if row.Kind != string(apitypes.WorkloadKindPod) {
		return PodView{}, ErrNotFound
	}
	out := PodView{
		Workload: row.ID, Name: row.Name, App: row.AppName, Ambiguous: row.SameName > 1,
		Active: row.DesiredState == "active" && row.AppState == "active",
	}
	if row.Spec != nil {
		if err := json.Unmarshal(row.Spec, &out.Spec); err != nil {
			return PodView{}, fmt.Errorf("decode release spec: %w", err)
		}
	}
	container, err := e.queries.DevboxContainer(ctx, workload)
	switch {
	case errors.Is(err, pgx.ErrNoRows):
	case err != nil:
		return PodView{}, fmt.Errorf("read pod container: %w", err)
	default:
		id := ContainerID(container.ID)
		out.Container = &id
		out.Connections = int(container.Connections)
		out.Phase = livePhase(ContainerState(container.State), container.HostID != nil, container.Stages, out.Spec.Disks != nil && len(*out.Spec.Disks) > 0)
		if out.Connections == 0 && container.KeepWarmSeconds != nil && container.State == string(ContainerReady) {
			deadline := container.LastConnection.Add(time.Duration(*container.KeepWarmSeconds) * time.Second)
			if container.ActiveUntil != nil && container.ActiveUntil.After(deadline) {
				deadline = *container.ActiveUntil
			}
			out.IdleDeadline = &deadline
		}
		return out, nil
	}
	saving, err := savingDisk(out.Spec)
	if err != nil {
		return PodView{}, err
	}
	state, err := e.queries.PodState(ctx, workload)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return PodView{}, fmt.Errorf("read pod state: %w", err)
	}
	woken := err == nil && !state.Parked && state.WokenAt != nil && time.Since(*state.WokenAt) < podWakeWindow
	failure, err := e.queries.DevboxFailure(ctx, workload)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return PodView{}, fmt.Errorf("read pod failure: %w", err)
	}
	failed := err == nil
	switch {
	case saving:
		out.Phase = apitypes.DevboxPhaseStopping
	case failed && woken:
		out.Phase, out.Reason = apitypes.DevboxPhaseFailed, failure.Reason
	case woken && out.Active:
		out.Phase = apitypes.DevboxPhaseQueued
	case failed:
		out.Phase, out.Reason = apitypes.DevboxPhaseFailed, failure.Reason
	default:
		out.Phase = apitypes.DevboxPhaseStopped
	}
	if failed {
		id := ContainerID(failure.ID)
		out.Failed = &id
	}
	return out, nil
}

// livePhase is what a live container is doing: placed or not, then the
// first start stage its host has not finished.
func livePhase(state ContainerState, placed bool, stages []string, disks bool) apitypes.DevboxPhase {
	switch {
	case state == ContainerReady:
		return apitypes.DevboxPhaseRunning
	case state == ContainerDraining:
		return apitypes.DevboxPhaseStopping
	case !placed:
		return apitypes.DevboxPhaseQueued
	case !slices.Contains(stages, "image"):
		return apitypes.DevboxPhasePullingImage
	case disks && !slices.Contains(stages, "disk"):
		return apitypes.DevboxPhaseRestoringDisk
	}
	return apitypes.DevboxPhaseStarting
}
