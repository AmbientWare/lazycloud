package execution

import (
	"context"
	"encoding/json"
	"fmt"
	"math"

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
	// ExitCode is how a pod's command exited, for StopExited.
	ExitCode *int
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
		ID: row.ID, StopReason: ptr(string(exit.Reason)), ExitMessage: &message, ExitCode: int32Of(exit.ExitCode),
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

	if row.ReleaseID == nil {
		// A build container: the images owner decides what its stop means.
		return e.notifyStopped(ctx, tx, row.HostID, database.ChannelImageBuild, row.ImageBuildID.String())
	}
	release := *row.ReleaseID

	var failQueued *Failure
	switch exit.Reason {
	case StopLoadError:
		failQueued = exit.LoadError
		if failQueued == nil {
			failQueued = &Failure{Kind: FailureLoadError, Message: exit.Message}
		}
		if err := q.RecordLoadError(ctx, RecordLoadErrorParams{ID: release, LoadError: &failQueued.Message}); err != nil {
			return fmt.Errorf("record load error: %w", err)
		}
	case StopStartFailed:
		failures, err := q.CountStartFailure(ctx, release)
		if err != nil {
			return fmt.Errorf("count start failure: %w", err)
		}
		if failures >= startFailureLimit {
			failQueued = &Failure{Kind: FailureStartFailed, Message: exit.Message}
		}
	case StopRequested, StopCrashed, StopOutOfMemory, StopHostLost, StopExited:
	}
	if failQueued != nil {
		encoded, err := json.Marshal(failQueued)
		if err != nil {
			return fmt.Errorf("encode failure: %w", err)
		}
		queued, err := e.lockQueuedWithDependents(ctx, q, release, math.MaxInt32)
		if err != nil {
			return err
		}
		failed, err := q.FailQueuedTasksOfRelease(ctx, FailQueuedTasksOfReleaseParams{Ids: queued, Failure: encoded})
		if err != nil {
			return fmt.Errorf("fail queued tasks: %w", err)
		}
		if err := recordCallbacks(ctx, q, CallbackFailed, failed, nil); err != nil {
			return err
		}
		if err := notifyAll(ctx, tx, database.ChannelTask, uuidStrings(failed)); err != nil {
			return err
		}
		if err := e.resolveDependents(ctx, tx, failed, upstreamUnsuccessful); err != nil {
			return err
		}
	}

	return e.notifyStopped(ctx, tx, row.HostID, database.ChannelExecution, release.String())
}

// notifyStopped wakes the container's host and the container's owner.
func (e *Execution) notifyStopped(ctx context.Context, tx pgx.Tx, host *uuid.UUID, owner database.Channel, id string) error {
	if host != nil {
		if err := database.Notify(ctx, tx, database.ChannelHost, host.String()); err != nil {
			return err
		}
	}
	return database.Notify(ctx, tx, owner, id)
}

func ptr[T any](v T) *T { return &v }

func int32Of(v *int) *int32 {
	if v == nil {
		return nil
	}
	n := int32(*v) //nolint:gosec // Exit codes and versions fit.
	return &n
}
