package execution

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// ErrTaskNotRunningHere means a container API request named a task whose
// current attempt does not run on the calling container.
var ErrTaskNotRunningHere = errors.New("the task is not running on this container")

// ContainerPrincipal resolves the caller of a container API request: the
// container must be assigned to host and not stopped. When task is set, the
// task's current attempt must run on the container; the principal then
// carries the attempt and the root of the task's call graph.
func (e *Execution) ContainerPrincipal(ctx context.Context, host compute.HostID, container ContainerID, task *TaskID) (identity.ContainerPrincipal, error) {
	row, err := e.queries.ContainerAuthority(ctx, ContainerAuthorityParams{ID: uuid.UUID(container), HostID: hostUUID(host)})
	if errors.Is(err, pgx.ErrNoRows) {
		return identity.ContainerPrincipal{}, ErrNotAssigned
	}
	if err != nil {
		return identity.ContainerPrincipal{}, fmt.Errorf("read container authority: %w", err)
	}
	switch ContainerState(row.State) {
	case ContainerStarting, ContainerReady, ContainerDraining:
	case ContainerPending, ContainerStopped:
		return identity.ContainerPrincipal{}, ErrNotAssigned
	}
	p := identity.ContainerPrincipal{
		Container: uuid.UUID(container),
		Workspace: identity.Workspace{
			ID: identity.WorkspaceID(row.WorkspaceID), Name: row.WorkspaceName, State: identity.WorkspaceState(row.WorkspaceState),
		},
	}
	if task == nil {
		return p, nil
	}
	running, err := e.queries.RunningTaskOnContainer(ctx, RunningTaskOnContainerParams{
		TaskID: uuid.UUID(*task), ContainerID: uuid.UUID(container),
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return identity.ContainerPrincipal{}, ErrTaskNotRunningHere
	}
	if err != nil {
		return identity.ContainerPrincipal{}, fmt.Errorf("read running task: %w", err)
	}
	id := uuid.UUID(*task)
	p.Task, p.Attempt, p.RootTask = &id, &running.AttemptID, &running.RootTaskID
	return p, nil
}

// StartFailed stops a starting container the server could not build a
// start for, such as one whose release names a missing secret. It counts
// toward the release's start failure limit like a failed preparation.
func (e *Execution) StartFailed(ctx context.Context, host compute.HostID, container ContainerID, message string) error {
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		state, err := e.queries.WithTx(tx).LockStartingContainer(ctx, LockStartingContainerParams{
			ID: uuid.UUID(container), HostID: hostUUID(host),
		})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotAssigned
		}
		if err != nil {
			return fmt.Errorf("lock container: %w", err)
		}
		if ContainerState(state) != ContainerStarting {
			return nil
		}
		return e.containerExited(ctx, tx, container, ContainerExit{Reason: StopStartFailed, Message: message})
	})
	if err != nil {
		return fmt.Errorf("fail start of container %s: %w", container, err)
	}
	return nil
}

// CallbackEvent is the transition a task callback reports.
type CallbackEvent string

const (
	CallbackRetry     CallbackEvent = "retry"
	CallbackSucceeded CallbackEvent = "succeeded"
	CallbackFailed    CallbackEvent = "failed"
	CallbackCancelled CallbackEvent = "cancelled"
)

// recordCallbacks records in q's transaction a callback for each task whose
// release has a callback_url. failure is the attempt failure behind a retry.
func recordCallbacks(ctx context.Context, q *Queries, event CallbackEvent, tasks []uuid.UUID, failure *Failure) error {
	if len(tasks) == 0 {
		return nil
	}
	var encoded []byte
	if failure != nil {
		var err error
		if encoded, err = json.Marshal(failure); err != nil {
			return fmt.Errorf("encode callback failure: %w", err)
		}
	}
	if err := q.EnqueueCallbacks(ctx, EnqueueCallbacksParams{Event: string(event), TaskIds: tasks, Failure: encoded}); err != nil {
		return fmt.Errorf("enqueue %s callbacks: %w", event, err)
	}
	return nil
}
