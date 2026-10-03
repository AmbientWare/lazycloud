package execution

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/compute"
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

// Completion is how an attempt a host ran on Container ended.
type Completion struct {
	Container ContainerID
	Outcome   AttemptOutcome
}

// CompleteAttempt records one completion; see CompleteAttempts.
func (e *Execution) CompleteAttempt(ctx context.Context, host compute.HostID, container ContainerID, outcome AttemptOutcome) error {
	return e.CompleteAttempts(ctx, host, []Completion{{Container: container, Outcome: outcome}})[0]
}

// CompleteAttempts records how attempts that host ran ended, in one
// transaction, and returns one error per completion. Only a running attempt
// on the completion's container, assigned to host, accepts an outcome;
// anything else gets ErrStaleAttempt and changes nothing. A result above
// MaxPayloadBytes fails the attempt instead. When the transaction fails,
// each completion is written again in its own, so a failure stays with the
// completion that caused it.
func (e *Execution) CompleteAttempts(ctx context.Context, host compute.HostID, completions []Completion) []error {
	containers := make([]ContainerID, len(completions))
	outcomes := make([]AttemptOutcome, len(completions))
	for n, c := range completions {
		containers[n], outcomes[n] = c.Container, c.Outcome
		if o := c.Outcome; o.Result != nil && len(o.Result.Data) > MaxPayloadBytes {
			outcomes[n] = AttemptOutcome{
				Attempt: o.Attempt,
				State:   AttemptFailed,
				Failure: &Failure{Kind: FailureUserError, Type: "ResultTooLarge", Message: ErrPayloadTooLarge.Error()},
			}
		}
	}
	var stale []bool
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		var err error
		stale, err = e.finishAttempts(ctx, tx, &host, containers, outcomes)
		return err
	})
	errs := make([]error, len(completions))
	switch {
	case err == nil:
		for n, s := range stale {
			if s {
				errs[n] = fmt.Errorf("complete attempt %s: %w", outcomes[n].Attempt, ErrStaleAttempt)
			}
		}
	case len(completions) == 1 || ctx.Err() != nil:
		for n := range errs {
			errs[n] = fmt.Errorf("complete attempt %s: %w", outcomes[n].Attempt, err)
		}
	default:
		for n := range completions {
			errs[n] = e.CompleteAttempts(ctx, host, completions[n:n+1])[0]
		}
	}
	return errs
}
