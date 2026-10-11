package execution

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
)

// RetryStarts applies a user action, such as a resume, deploy, task or
// request, to releases in tx. Background restarts stop at the start failure
// limit; only these actions start a stopped release again. A release
// stopped by a load error or the limit gets one fresh start: its load error
// is cleared and its count set one short of the limit, so another failure
// stops it at once. A release whose newest container stopped within the
// backoff at the limit is held instead, with that start's Failure, so
// traffic retries it at most once per window. The returned map holds the
// held releases. The row lock lets one concurrent action retry; the rest
// see the fresh count and wait for that start.
func RetryStarts(ctx context.Context, tx pgx.Tx, releases []uuid.UUID) (map[uuid.UUID]*Failure, error) {
	held := map[uuid.UUID]*Failure{}
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
			continue
		}
		held[row.ID] = &Failure{Kind: FailureStartFailed, Message: row.Reason}
		if row.LoadError != nil {
			held[row.ID] = &Failure{Kind: FailureLoadError, Message: *row.LoadError}
		}
	}
	if len(retried) == 0 {
		return held, nil
	}
	if err := q.RetryReleaseStarts(ctx, RetryReleaseStartsParams{StartFailures: startFailureLimit - 1, Ids: retried}); err != nil {
		return nil, fmt.Errorf("retry release starts: %w", err)
	}
	if err := database.NotifyAll(ctx, tx, database.ChannelExecution, uuidStrings(retried)); err != nil {
		return nil, err
	}
	return held, nil
}

// RetryStart applies a user action to one release in its own transaction:
// the Failure it is held on, or nil to go ahead.
func (e *Execution) RetryStart(ctx context.Context, release uuid.UUID) (*Failure, error) {
	var held *Failure
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		retries, err := RetryStarts(ctx, tx, []uuid.UUID{release})
		held = retries[release]
		return err
	})
	if err != nil {
		return nil, fmt.Errorf("retry start: %w", err)
	}
	return held, nil
}
