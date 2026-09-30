package execution

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
)

// ErrStaleAttempt means the attempt is no longer running on the caller's
// container, so its outcome is discarded.
var ErrStaleAttempt = errors.New("attempt is no longer running")

// Payload is an encoded task input or result.
type Payload struct {
	Encoding Encoding
	Data     []byte
}

// AttemptOutcome is how an attempt ended.
type AttemptOutcome struct {
	Attempt AttemptID
	// State is any state except AttemptRunning.
	State AttemptState
	// Result is set when State is AttemptSucceeded.
	Result *Payload
	// Failure is set when State is AttemptFailed, AttemptTimedOut or
	// AttemptLost.
	Failure *Failure
}

// finishAttempt records outcome in tx and advances the task: success stores
// the result, a retryable failure with attempts left requeues after the
// release's retry delay, anything else fails the task. When host is set the
// attempt's container must be assigned to it.
func (e *Execution) finishAttempt(ctx context.Context, tx pgx.Tx, host *compute.HostID, outcome AttemptOutcome) error {
	q := e.queries.WithTx(tx)
	attemptID := uuid.UUID(outcome.Attempt)
	task, err := q.LockTaskForAttempt(ctx, attemptID)
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrStaleAttempt
	}
	if err != nil {
		return fmt.Errorf("lock task: %w", err)
	}
	attempt, err := q.LockRunningAttempt(ctx, attemptID)
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrStaleAttempt
	}
	if err != nil {
		return fmt.Errorf("lock attempt: %w", err)
	}
	if host != nil && (attempt.HostID == nil || *attempt.HostID != uuid.UUID(*host)) {
		return ErrStaleAttempt
	}
	if err := q.SetAttemptState(ctx, SetAttemptStateParams{ID: attemptID, State: string(outcome.State)}); err != nil {
		return fmt.Errorf("set attempt state: %w", err)
	}
	if TaskStatus(task.Status) != TaskRunning || task.CurrentAttemptID == nil || *task.CurrentAttemptID != attemptID {
		// The task moved on, for example through cancellation; only the
		// attempt row records this outcome.
		return nil
	}

	switch outcome.State {
	case AttemptSucceeded:
		if outcome.Result == nil {
			return errors.New("succeeded attempt without a result")
		}
		if err := q.InsertTaskResult(ctx, InsertTaskResultParams{
			TaskID: task.ID, Encoding: string(outcome.Result.Encoding), Data: outcome.Result.Data,
		}); err != nil {
			return fmt.Errorf("insert result: %w", err)
		}
		if err := q.SucceedTask(ctx, task.ID); err != nil {
			return fmt.Errorf("succeed task: %w", err)
		}
	case AttemptFailed, AttemptTimedOut, AttemptLost:
		if outcome.Failure == nil {
			return fmt.Errorf("%s attempt without a failure", outcome.State)
		}
		if outcome.Failure.Kind.Retryable() && int(task.AttemptCount) < int(task.MaxAttempts) {
			var spec apitypes.FunctionSpec
			if err := json.Unmarshal(task.Spec, &spec); err != nil {
				return fmt.Errorf("decode release spec: %w", err)
			}
			delay := RetryPolicyOf(spec).NextAttemptDelay(int(task.AttemptCount) + 1)
			if err := q.RequeueTask(ctx, RequeueTaskParams{ID: task.ID, DelaySeconds: delay.Seconds()}); err != nil {
				return fmt.Errorf("requeue task: %w", err)
			}
			if err := database.Notify(ctx, tx, database.ChannelClaim, task.ReleaseID.String()); err != nil {
				return err
			}
		} else if err := e.failTask(ctx, q, task.ID, *outcome.Failure); err != nil {
			return err
		}
	case AttemptCancelled:
		// Cancellation finishes the task itself before cancelling the attempt.
		return nil
	case AttemptRunning:
		return errors.New("an outcome cannot be running")
	}
	if err := database.Notify(ctx, tx, database.ChannelTask, task.ID.String()); err != nil {
		return err
	}
	return database.Notify(ctx, tx, database.ChannelExecution, task.ReleaseID.String())
}

func (e *Execution) failTask(ctx context.Context, q *Queries, task uuid.UUID, failure Failure) error {
	encoded, err := json.Marshal(failure)
	if err != nil {
		return fmt.Errorf("encode failure: %w", err)
	}
	if err := q.FailTask(ctx, FailTaskParams{ID: task, Failure: encoded}); err != nil {
		return fmt.Errorf("fail task: %w", err)
	}
	return nil
}
