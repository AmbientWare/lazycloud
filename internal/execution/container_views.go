package execution

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/cpu"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Container is a container as callers see it.
type Container struct {
	ID       ContainerID
	App      string
	Function string
	Release  uuid.UUID
	// Version is the release's deployed version, nil for a working-tree
	// release.
	Version      *int
	State        ContainerState
	StopReason   *StopReason
	ExitMessage  *string
	Slots        int
	RunningTasks int
	CPUMillis    cpu.Millis
	MemoryBytes  int64
	CreatedAt    time.Time
	ReadyAt      *time.Time
	StoppedAt    *time.Time
	Kind         apitypes.WorkloadKind
	Purpose      ContainerPurpose
	ExitCode     *int
	GPUCount     int
	// Host is the name of the host the container was placed on.
	Host *string
	// ExpiresAt is when an instance with a timeout stops unless it is used
	// again.
	ExpiresAt *time.Time
	// Image is the image reference the release runs, empty when the
	// release names none.
	Image string
}

// ContainerFilter narrows a container listing by app name or by workload.
type ContainerFilter struct {
	Live     bool
	App      *string
	Workload *uuid.UUID
}

// ContainerPage is one page of containers, newest first.
type ContainerPage struct {
	Containers []Container
	Next       string
}

// ListContainers returns the workspace's containers newest first, only live
// ones or one app's or workload's when the filter says so.
func (e *Execution) ListContainers(ctx context.Context, workspace identity.WorkspaceID, filter ContainerFilter, limit int, cursor string) (ContainerPage, error) {
	before := uuid.Max
	if cursor != "" {
		id, err := uuid.Parse(cursor)
		if err != nil {
			return ContainerPage{}, ErrInvalidCursor
		}
		before = id
	}
	size := pageSize(limit)
	ids, err := e.containerIDs(ctx, workspace, filter, before, size+1)
	if err != nil {
		return ContainerPage{}, fmt.Errorf("list containers: %w", err)
	}
	page := ContainerPage{Containers: []Container{}}
	if len(ids) > int(size) {
		ids = ids[:size]
		page.Next = ids[len(ids)-1].String()
	}
	if len(ids) == 0 {
		return page, nil
	}
	if page.Containers, err = e.containerViews(ctx, workspace, ids); err != nil {
		return ContainerPage{}, err
	}
	return page, nil
}

// containerIDs reads the ids of a page of the filter's containers below
// before, newest first, from one index range for the workspace or per
// workload.
func (e *Execution) containerIDs(ctx context.Context, workspace identity.WorkspaceID, filter ContainerFilter, before uuid.UUID, rows int32) ([]uuid.UUID, error) {
	ws := uuid.UUID(workspace)
	byWorkload := filter.App != nil || filter.Workload != nil
	switch {
	case filter.Live && byWorkload:
		return e.queries.LiveWorkloadContainerIDs(ctx, LiveWorkloadContainerIDsParams{
			Before: before, MaxRows: rows, WorkspaceID: ws, App: filter.App, WorkloadID: filter.Workload,
		})
	case filter.Live:
		return e.queries.LiveWorkspaceContainerIDs(ctx, LiveWorkspaceContainerIDsParams{WorkspaceID: ws, Before: before, MaxRows: rows})
	case byWorkload:
		return e.queries.WorkloadContainerIDs(ctx, WorkloadContainerIDsParams{
			Before: before, MaxRows: rows, WorkspaceID: ws, App: filter.App, WorkloadID: filter.Workload,
		})
	default:
		return e.queries.WorkspaceContainerIDs(ctx, WorkspaceContainerIDsParams{WorkspaceID: ws, Before: before, MaxRows: rows})
	}
}

// GetContainer reads a container of the workspace.
func (e *Execution) GetContainer(ctx context.Context, workspace identity.WorkspaceID, id ContainerID) (Container, error) {
	containers, err := e.containerViews(ctx, workspace, []uuid.UUID{uuid.UUID(id)})
	if err != nil {
		return Container{}, err
	}
	if len(containers) == 0 {
		return Container{}, ErrNotFound
	}
	return containers[0], nil
}

// containerViews reads the workspace's function containers of ids, newest
// first.
func (e *Execution) containerViews(ctx context.Context, workspace identity.WorkspaceID, ids []uuid.UUID) ([]Container, error) {
	rows, err := e.queries.ContainerViews(ctx, ContainerViewsParams{Ids: ids, WorkspaceID: uuid.UUID(workspace)})
	if err != nil {
		return nil, fmt.Errorf("read containers: %w", err)
	}
	containers := make([]Container, len(rows))
	for n, row := range rows {
		containers[n] = containerFrom(row)
	}
	return containers, nil
}

func containerFrom(row ContainerViewsRow) Container {
	c := Container{
		ID: ContainerID(row.ID), App: row.AppName, Function: row.FunctionName, Release: row.ReleaseID,
		Version: versionOf(row.Version), State: ContainerState(row.State), ExitMessage: row.ExitMessage,
		Slots: int(row.Slots), RunningTasks: int(row.Running), CPUMillis: row.CpuMillis, MemoryBytes: row.MemoryBytes,
		CreatedAt: row.CreatedAt, ReadyAt: row.ReadyAt, StoppedAt: row.StoppedAt,
		Kind: apitypes.WorkloadKind(row.Kind), Purpose: ContainerPurpose(row.Purpose), ExitCode: intOf(row.ExitCode),
		GPUCount: int(row.GpuCount), Host: row.HostName, Image: row.Image,
	}
	if row.StopReason != nil {
		r := StopReason(*row.StopReason)
		c.StopReason = &r
	}
	if row.KeepWarmSeconds != nil {
		c.ExpiresAt = row.ActiveUntil
	}
	return c
}

// StopContainer stops a container now. A pending one stops at once. A
// starting, ready or draining one drains, and its running attempts end as
// lost and retry by policy, so its host stops it without waiting for them.
// Planning starts a replacement when the release still has work.
func (e *Execution) StopContainer(ctx context.Context, workspace identity.WorkspaceID, id ContainerID) (Container, error) {
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		return e.stopContainer(ctx, tx, workspace, id, stopCause{exit: "stopped by request", lost: "the container was stopped by request"})
	})
	if err != nil {
		return Container{}, fmt.Errorf("stop container %s: %w", id, err)
	}
	return e.GetContainer(ctx, workspace, id)
}

// stopCause is what a stop tells the container's exit and its attempts.
type stopCause struct{ exit, lost string }

func (e *Execution) stopContainer(ctx context.Context, tx pgx.Tx, workspace identity.WorkspaceID, id ContainerID, cause stopCause) error {
	q := e.queries.WithTx(tx)
	row, err := q.LockContainerInWorkspace(ctx, LockContainerInWorkspaceParams{ID: uuid.UUID(id), WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	}
	if err != nil {
		return fmt.Errorf("lock container: %w", err)
	}
	switch ContainerState(row.State) {
	case ContainerStopped:
		return nil
	case ContainerPending:
		return e.containerExited(ctx, tx, id, ContainerExit{Reason: StopRequested, Message: cause.exit})
	case ContainerStarting, ContainerReady, ContainerDraining:
	}
	if err := q.DrainContainer(ctx, row.ID); err != nil {
		return fmt.Errorf("drain container: %w", err)
	}
	attempts, err := q.RunningAttemptsOnContainer(ctx, row.ID)
	if err != nil {
		return fmt.Errorf("list running attempts: %w", err)
	}
	outcomes := make([]AttemptOutcome, len(attempts))
	for n, attempt := range attempts {
		outcomes[n] = AttemptOutcome{
			Attempt: AttemptID(attempt), State: AttemptLost,
			Failure: &Failure{Kind: FailureLost, Message: cause.lost},
		}
	}
	if _, err := e.finishAttempts(ctx, tx, nil, nil, outcomes); err != nil {
		return err
	}
	if row.HostID != nil {
		if err := database.Notify(ctx, tx, database.ChannelHost, row.HostID.String()); err != nil {
			return err
		}
	}
	return database.Notify(ctx, tx, database.ChannelExecution, row.ReleaseID.String())
}
