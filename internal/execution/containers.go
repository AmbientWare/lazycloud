package execution

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
)

// startFailureLimit is how many consecutive containers of a release may fail
// preparation before its queued tasks fail instead of waiting for another.
const startFailureLimit = 3

// ContainerExit describes why a container stopped.
type ContainerExit struct {
	Reason  StopReason
	Message string
	// LoadError is the handler import failure for StopLoadError.
	LoadError *Failure
}

// containerExited stops a container in tx. Its running attempts are lost and
// retried by policy. A load error fails the release's queued tasks with that
// error, as does the start_failed limit. Stopping a stopped container does
// nothing, so duplicate reports are harmless. Lock order: container, then
// task, then attempt.
func (e *Execution) containerExited(ctx context.Context, tx pgx.Tx, container ContainerID, exit ContainerExit) error {
	q := e.queries.WithTx(tx)
	row, err := q.LockContainer(ctx, uuid.UUID(container))
	if err != nil {
		return fmt.Errorf("lock container: %w", err)
	}
	if ContainerState(row.State) == ContainerStopped {
		return nil
	}
	message := exit.Message
	if err := q.StopContainer(ctx, StopContainerParams{
		ID: row.ID, StopReason: ptr(string(exit.Reason)), ExitMessage: &message,
	}); err != nil {
		return fmt.Errorf("stop container: %w", err)
	}

	attempts, err := q.RunningAttemptsOnContainer(ctx, row.ID)
	if err != nil {
		return fmt.Errorf("list running attempts: %w", err)
	}
	for _, attempt := range attempts {
		lost := &Failure{Kind: FailureLost, Message: fmt.Sprintf("container stopped: %s", exit.Reason)}
		if exit.Message != "" {
			lost.Message += ": " + exit.Message
		}
		if err := e.finishAttempt(ctx, tx, nil, AttemptOutcome{Attempt: AttemptID(attempt), State: AttemptLost, Failure: lost}); err != nil {
			return err
		}
	}

	var failQueued *Failure
	switch exit.Reason {
	case StopLoadError:
		failQueued = exit.LoadError
		if failQueued == nil {
			failQueued = &Failure{Kind: FailureLoadError, Message: exit.Message}
		}
		if err := q.RecordLoadError(ctx, RecordLoadErrorParams{ID: row.ReleaseID, LoadError: &failQueued.Message}); err != nil {
			return fmt.Errorf("record load error: %w", err)
		}
	case StopStartFailed:
		failures, err := q.CountStartFailure(ctx, row.ReleaseID)
		if err != nil {
			return fmt.Errorf("count start failure: %w", err)
		}
		if failures >= startFailureLimit {
			failQueued = &Failure{Kind: FailureStartFailed, Message: exit.Message}
		}
	case StopRequested, StopCrashed, StopOutOfMemory, StopHostLost:
	}
	if failQueued != nil {
		encoded, err := json.Marshal(failQueued)
		if err != nil {
			return fmt.Errorf("encode failure: %w", err)
		}
		failed, err := q.FailQueuedTasksOfRelease(ctx, FailQueuedTasksOfReleaseParams{ReleaseID: row.ReleaseID, Failure: encoded})
		if err != nil {
			return fmt.Errorf("fail queued tasks: %w", err)
		}
		for _, task := range failed {
			if err := database.Notify(ctx, tx, database.ChannelTask, task.String()); err != nil {
				return err
			}
		}
	}

	if row.HostID != nil {
		if err := database.Notify(ctx, tx, database.ChannelHost, row.HostID.String()); err != nil {
			return err
		}
	}
	return database.Notify(ctx, tx, database.ChannelExecution, row.ReleaseID.String())
}

func ptr[T any](v T) *T { return &v }
