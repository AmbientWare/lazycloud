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
	var rows []ContainerViewRow
	if filter.Live {
		r, err := e.queries.ListLiveContainers(ctx, ListLiveContainersParams{
			WorkspaceID: uuid.UUID(workspace), App: filter.App, Before: before,
			WorkloadID: filter.Workload, MaxRows: size + 1,
		})
		if err != nil {
			return ContainerPage{}, fmt.Errorf("list live containers: %w", err)
		}
		for _, row := range r {
			rows = append(rows, ContainerViewRow(row))
		}
	} else {
		r, err := e.queries.ListContainers(ctx, ListContainersParams{
			WorkspaceID: uuid.UUID(workspace), App: filter.App, Before: before,
			WorkloadID: filter.Workload, MaxRows: size + 1,
		})
		if err != nil {
			return ContainerPage{}, fmt.Errorf("list containers: %w", err)
		}
		for _, row := range r {
			rows = append(rows, ContainerViewRow(row))
		}
	}
	var page ContainerPage
	if len(rows) > int(size) {
		rows = rows[:size]
		page.Next = rows[len(rows)-1].ID.String()
	}
	page.Containers = make([]Container, len(rows))
	for n, row := range rows {
		page.Containers[n] = containerFrom(row)
	}
	return page, nil
}

// GetContainer reads a container of the workspace.
func (e *Execution) GetContainer(ctx context.Context, workspace identity.WorkspaceID, id ContainerID) (Container, error) {
	row, err := e.queries.ContainerView(ctx, ContainerViewParams{WorkspaceID: uuid.UUID(workspace), ID: uuid.UUID(id)})
	if errors.Is(err, pgx.ErrNoRows) {
		return Container{}, ErrNotFound
	}
	if err != nil {
		return Container{}, fmt.Errorf("read container: %w", err)
	}
	return containerFrom(row), nil
}

func containerFrom(row ContainerViewRow) Container {
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
