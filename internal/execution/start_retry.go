package execution

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
)

// StartOutcome is what an explicit user action, such as a resume, a deploy,
// a task or a request, did to a release. Background restarts stop at the
// start failure limit; only these actions start a stopped release again.
type StartOutcome string

const (
	// StartAllowed means the release had not stopped starting; nothing
	// changed.
	StartAllowed StartOutcome = "allowed"
	// StartRetried means the release had stopped and now has one fresh
	// start: its load error is cleared and its count left one short of the
	// limit, so a failure stops it again at once.
	StartRetried StartOutcome = "retried"
	// StartHeld means the release stopped and its last start failed within
	// the backoff window, so the action fails with that start's error
	// instead of starting another.
	StartHeld StartOutcome = "held"
)

// StartRetry is the outcome for one release.
type StartRetry struct {
	Outcome StartOutcome
	// Failure is why the last start failed, for StartHeld.
	Failure *Failure
}

// RetryStarts applies a user action to releases in tx. A release stopped by
// a load error or the start failure limit gets one fresh start, unless its
// newest container stopped within the backoff of a release at the limit:
// that bounds user retries to one per window however much traffic arrives.
// The row lock lets one concurrent action retry; the rest see the fresh
// count and wait for that start.
func RetryStarts(ctx context.Context, tx pgx.Tx, releases []uuid.UUID) (map[uuid.UUID]StartRetry, error) {
	out := make(map[uuid.UUID]StartRetry, len(releases))
	for _, id := range releases {
		out[id] = StartRetry{Outcome: StartAllowed}
	}
	q := New(tx)
	rows, err := q.LockStoppedReleases(ctx, LockStoppedReleasesParams{Ids: releases, StartFailureLimit: startFailureLimit})
	if err != nil {
		return nil, fmt.Errorf("lock stopped releases: %w", err)
	}
	var retried []uuid.UUID
	for _, row := range rows {
		window := startRetryDelay(max(row.StartFailures, startFailureLimit))
		if row.StoppedAt == nil || time.Since(*row.StoppedAt) >= window {
			retried = append(retried, row.ID)
			out[row.ID] = StartRetry{Outcome: StartRetried}
			continue
		}
		failure := &Failure{Kind: FailureStartFailed, Message: row.Reason}
		if row.LoadError != nil {
			failure = &Failure{Kind: FailureLoadError, Message: *row.LoadError}
		}
		out[row.ID] = StartRetry{Outcome: StartHeld, Failure: failure}
	}
	if len(retried) == 0 {
		return out, nil
	}
	if err := q.RetryReleaseStarts(ctx, RetryReleaseStartsParams{StartFailures: startFailureLimit - 1, Ids: retried}); err != nil {
		return nil, fmt.Errorf("retry release starts: %w", err)
	}
	if err := database.NotifyAll(ctx, tx, database.ChannelExecution, uuidStrings(retried)); err != nil {
		return nil, err
	}
	return out, nil
}

// RetryStart applies a user action to one release in its own transaction.
func (e *Execution) RetryStart(ctx context.Context, release uuid.UUID) (StartRetry, error) {
	var out StartRetry
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		retries, err := RetryStarts(ctx, tx, []uuid.UUID{release})
		out = retries[release]
		return err
	})
	if err != nil {
		return StartRetry{}, fmt.Errorf("retry start: %w", err)
	}
	return out, nil
}
