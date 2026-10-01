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
func (e *Execution) CancelTask(ctx context.Context, workspace identity.WorkspaceID, id TaskID) (Task, error) {
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		task, err := q.LockTaskForCancel(ctx, LockTaskForCancelParams{ID: uuid.UUID(id), WorkspaceID: uuid.UUID(workspace)})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock task: %w", err)
		}
		status := TaskStatus(task.Status)
		if status.Terminal() {
			return nil
		}
		if err := q.CancelTask(ctx, task.ID); err != nil {
			return fmt.Errorf("cancel task: %w", err)
		}
		if err := recordCallbacks(ctx, q, CallbackCancelled, []uuid.UUID{task.ID}, nil); err != nil {
			return err
		}
		if status == TaskRunning && task.CurrentAttemptID != nil {
			attempt := *task.CurrentAttemptID
			// The task is already cancelled, so finishAttempt only records
			// the attempt's outcome.
			err := e.finishAttempt(ctx, tx, nil, AttemptOutcome{Attempt: AttemptID(attempt), State: AttemptCancelled})
			if err != nil && !errors.Is(err, ErrStaleAttempt) {
				return err
			}
			host, err := q.AttemptHost(ctx, attempt)
			if err != nil {
				return fmt.Errorf("read attempt host: %w", err)
			}
			if host != nil {
				if err := database.Notify(ctx, tx, database.ChannelHost, host.String()); err != nil {
					return err
				}
			}
		}
		if err := database.Notify(ctx, tx, database.ChannelTask, task.ID.String()); err != nil {
			return err
		}
		return database.Notify(ctx, tx, database.ChannelExecution, task.ReleaseID.String())
	})
	if err != nil {
		return Task{}, fmt.Errorf("cancel task: %w", err)
	}
	return e.readTask(ctx, workspace, id)
}
