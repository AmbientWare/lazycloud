package execution

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// CancelTask cancels a queued or running task and returns it. A queued task
// is cancelled at once. A running task and its attempt are cancelled in one
// transaction, and the host is woken to kill the slot; the attempt's late
// completion is then stale. Cancelling a finished task changes nothing.
// Tasks waiting on this one fail.
func (e *Execution) CancelTask(ctx context.Context, workspace identity.WorkspaceID, id TaskID) (Task, error) {
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		_, err := e.cancelTask(ctx, tx, &workspace, id)
		return err
	})
	if err != nil {
		return Task{}, fmt.Errorf("cancel task: %w", err)
	}
	return e.readTask(ctx, workspace, id)
}

// cancelTask cancels the task in tx and reports whether it was queued or
// running. With workspace set the task must belong to it.
func (e *Execution) cancelTask(ctx context.Context, tx pgx.Tx, workspace *identity.WorkspaceID, id TaskID) (bool, error) {
	q := e.queries.WithTx(tx)
	params := LockTaskForCancelParams{ID: uuid.UUID(id)}
	if workspace != nil {
		ws := uuid.UUID(*workspace)
		params.WorkspaceID = &ws
	}
	task, err := q.LockTaskForCancel(ctx, params)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, ErrNotFound
	}
	if err != nil {
		return false, fmt.Errorf("lock task: %w", err)
	}
	status := TaskStatus(task.Status)
	if status.Terminal() {
		return false, nil
	}
	if err := q.CancelTask(ctx, task.ID); err != nil {
		return false, fmt.Errorf("cancel task: %w", err)
	}
	if err := recordCallbacks(ctx, q, CallbackCancelled, []uuid.UUID{task.ID}, nil); err != nil {
		return false, err
	}
	if status == TaskRunning && task.CurrentAttemptID != nil {
		attempt := *task.CurrentAttemptID
		// The task is already cancelled, so this only records the
		// attempt's outcome.
		if _, err := e.finishAttempts(ctx, tx, nil, nil, []AttemptOutcome{{Attempt: AttemptID(attempt), State: AttemptCancelled}}); err != nil {
			return false, err
		}
		host, err := q.AttemptHost(ctx, attempt)
		if err != nil {
			return false, fmt.Errorf("read attempt host: %w", err)
		}
		if host != nil {
			if err := database.Notify(ctx, tx, database.ChannelHost, host.String()); err != nil {
				return false, err
			}
		}
	}
	// Dependents lock after the attempt, keeping task then attempt order.
	if err := e.resolveDependents(ctx, tx, []uuid.UUID{task.ID}, upstreamUnsuccessful); err != nil {
		return false, err
	}
	if err := database.Notify(ctx, tx, database.ChannelTask, task.ID.String()); err != nil {
		return false, err
	}
	return true, database.Notify(ctx, tx, database.ChannelExecution, task.ReleaseID.String())
}

// StopResult says which requested tasks a stop cancelled.
type StopResult struct {
	Stopped []TaskID
	// Skipped were already finished or are not in the workspace.
	Skipped []TaskID
}

// StopTasks cancels each queued or running task of ids, each in its own
// transaction so one failure keeps the others' outcomes.
func (e *Execution) StopTasks(ctx context.Context, workspace identity.WorkspaceID, ids []TaskID) (StopResult, error) {
	result := StopResult{Stopped: []TaskID{}, Skipped: []TaskID{}}
	for _, id := range ids {
		var cancelled bool
		err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
			var err error
			cancelled, err = e.cancelTask(ctx, tx, &workspace, id)
			return err
		})
		switch {
		case err == nil && cancelled:
			result.Stopped = append(result.Stopped, id)
		case err == nil, errors.Is(err, ErrNotFound):
			result.Skipped = append(result.Skipped, id)
		default:
			return result, fmt.Errorf("stop task %s: %w", id, err)
		}
	}
	return result, nil
}
