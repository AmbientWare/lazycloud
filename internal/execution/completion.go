package execution

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
)

// CrashFailure is the failure of an attempt whose slot process died while
// running it. The platform did not lose the attempt; the user's process
// ended, as with a native crash, os._exit or the kernel killing it. It is a
// user error, so the release's retry policy applies as for a raised
// exception.
func CrashFailure(message string) Failure {
	if message == "" {
		message = "the runner process exited while running the attempt"
	}
	return Failure{Kind: FailureUserError, Type: "WorkerCrashed", Message: message}
}

// CompleteAttempt records how an attempt that host ran on container ended.
// Only a running attempt on that container, assigned to that host, accepts
// an outcome; anything else returns ErrStaleAttempt and changes nothing. A
// result above MaxPayloadBytes fails the attempt instead.
func (e *Execution) CompleteAttempt(ctx context.Context, host compute.HostID, container ContainerID, outcome AttemptOutcome) error {
	if outcome.Result != nil && len(outcome.Result.Data) > MaxPayloadBytes {
		outcome = AttemptOutcome{
			Attempt: outcome.Attempt,
			State:   AttemptFailed,
			Failure: &Failure{Kind: FailureUserError, Type: "ResultTooLarge", Message: ErrPayloadTooLarge.Error()},
		}
	}
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		owner, err := e.queries.WithTx(tx).AttemptContainer(ctx, uuid.UUID(outcome.Attempt))
		if errors.Is(err, pgx.ErrNoRows) || (err == nil && owner.ContainerID != uuid.UUID(container)) {
			return ErrStaleAttempt
		}
		if err != nil {
			return fmt.Errorf("read attempt container: %w", err)
		}
		if err := e.finishAttempt(ctx, tx, &host, outcome); err != nil {
			return err
		}
		if ContainerState(owner.ContainerState) == ContainerDraining {
			// The host may now stop the container.
			return database.Notify(ctx, tx, database.ChannelHost, host.String())
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("complete attempt %s: %w", outcome.Attempt, err)
	}
	return nil
}
