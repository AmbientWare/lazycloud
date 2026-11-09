package execution

import (
	"context"

	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
)

const (
	// recoveryBatch bounds the rows one recovery scan reads. A scan pages
	// through every due row, so items that keep failing never hide the rest.
	recoveryBatch = 100
	// StartTimeout is how long an assigned container may take to become ready
	// before it counts as a start failure.
	StartTimeout = 10 * time.Minute
)

// sweep pages through the due rows list returns after the last one it
// read, recoveryBatch at a time, so rows that keep failing never hide the
// rest. step handles each row in its own transaction and reports whether it
// changed it, and done logs one that changed. A row whose step fails is
// logged under key and id and the rest proceed, unless ctx ended.
func sweep[R any](ctx context.Context, e *Execution, logger *slog.Logger, what, key string, id func(R) any,
	list func(last *R) ([]R, error), step func(pgx.Tx, R) (bool, error), done func(R),
) error {
	var last *R
	for {
		rows, err := list(last)
		if err != nil {
			return fmt.Errorf("list for %s: %w", what, err)
		}
		for _, row := range rows {
			var changed bool
			err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
				var err error
				changed, err = step(tx, row)
				return err
			})
			switch {
			case err == nil && changed:
				done(row)
			case err == nil:
			case ctx.Err() != nil:
				return fmt.Errorf("%s: %w", what, err)
			default:
				logger.ErrorContext(ctx, what, key, id(row), "error", err)
			}
		}
		if len(rows) < recoveryBatch {
			return nil
		}
		last = &rows[len(rows)-1]
	}
}

// TimeOutAttempts ends every running attempt past its deadline as timed out,
// applies the retry policy and wakes the host so it kills the slot. Each
// attempt commits on its own.
func (e *Execution) TimeOutAttempts(ctx context.Context, logger *slog.Logger) error {
	return sweep(ctx, e, logger, "time out attempt", "attempt_id", func(a OverdueAttemptsRow) any { return a.ID },
		func(last *OverdueAttemptsRow) ([]OverdueAttemptsRow, error) {
			params := OverdueAttemptsParams{BatchSize: recoveryBatch}
			if last != nil {
				params.AfterDeadlineAt, params.AfterID = last.DeadlineAt, last.ID
			}
			return e.queries.OverdueAttempts(ctx, params)
		},
		func(tx pgx.Tx, attempt OverdueAttemptsRow) (bool, error) {
			stale, err := e.finishAttempts(ctx, tx, nil, nil, []AttemptOutcome{{
				Attempt: AttemptID(attempt.ID),
				State:   AttemptTimedOut,
				Failure: &Failure{Kind: FailureTimeout, Message: "attempt exceeded its timeout"},
			}})
			switch {
			case err != nil:
				return false, err
			case stale[0]:
				// It finished after the scan.
				return false, nil
			case attempt.HostID == nil:
				return true, nil
			}
			return true, database.Notify(ctx, tx, database.ChannelHost, attempt.HostID.String())
		},
		func(attempt OverdueAttemptsRow) {
			logger.InfoContext(ctx, "attempt timed out", "attempt_id", attempt.ID, "host_id", attempt.HostID)
		})
}

// TimeOutStarts stops as start failures the containers that stayed
// starting longer than StartTimeout, and the fleet containers no launched
// host took within the fleet's queue timeout.
func (e *Execution) TimeOutStarts(ctx context.Context, logger *slog.Logger) error {
	if err := e.timeOutPending(ctx, logger); err != nil {
		if ctx.Err() != nil {
			return err
		}
		// Starts time out on their own.
		logger.ErrorContext(ctx, "pending timeout pass", "error", err)
	}
	return sweep(ctx, e, logger, "time out container start", "container_id", func(c StuckStartingContainersRow) any { return c.ID },
		func(last *StuckStartingContainersRow) ([]StuckStartingContainersRow, error) {
			params := StuckStartingContainersParams{TimeoutSeconds: StartTimeout.Seconds(), BatchSize: recoveryBatch}
			if last != nil {
				// The query filters on assigned_at, so it is never null here.
				params.AfterAssignedAt, params.AfterID = *last.AssignedAt, last.ID
			}
			return e.queries.StuckStartingContainers(ctx, params)
		},
		func(tx pgx.Tx, container StuckStartingContainersRow) (bool, error) {
			n, err := e.queries.WithTx(tx).LockStuckStartingContainer(ctx, LockStuckStartingContainerParams{
				ID: container.ID, TimeoutSeconds: StartTimeout.Seconds(),
			})
			if err != nil || n != 1 {
				return false, err
			}
			return true, e.containerExited(ctx, tx, ContainerID(container.ID), ContainerExit{
				Reason:  StopStartFailed,
				Message: fmt.Sprintf("container did not become ready within %s", StartTimeout),
			})
		},
		func(container StuckStartingContainersRow) {
			logger.WarnContext(ctx, "container start timed out", "container_id", container.ID)
		})
}

// ReleaseLostHosts marks hosts silent for longer than
// compute.LivenessTimeout as lost and stops their live containers as
// host_lost in the same transaction, so their running attempts are
// preempted and retried. It locks every live container of the host in id
// order before finishing any attempt, so it never locks a container after a
// task. Each host commits on its own.
func (e *Execution) ReleaseLostHosts(ctx context.Context, logger *slog.Logger) error {
	containers := map[compute.HostID][]uuid.UUID{}
	return sweep(ctx, e, logger, "release lost host", "host_id", func(h compute.StaleHost) any { return h.ID },
		func(last *compute.StaleHost) ([]compute.StaleHost, error) {
			return compute.StaleHosts(ctx, e.pool, last, recoveryBatch)
		},
		func(tx pgx.Tx, host compute.StaleHost) (bool, error) {
			marked, err := compute.MarkLost(ctx, tx, host.ID)
			if err != nil || !marked {
				return false, err
			}
			hostID := uuid.UUID(host.ID)
			live, err := e.queries.WithTx(tx).LockLiveContainersOnHost(ctx, &hostID)
			if err != nil {
				return false, fmt.Errorf("lock live containers: %w", err)
			}
			containers[host.ID] = live
			for _, container := range live {
				if err := e.containerExited(ctx, tx, ContainerID(container), ContainerExit{
					Reason: StopHostLost, Message: "host stopped reporting",
				}); err != nil {
					return false, fmt.Errorf("stop container %s: %w", container, err)
				}
			}
			return true, nil
		},
		func(host compute.StaleHost) {
			live := containers[host.ID]
			logger.WarnContext(ctx, "host lost", "host_id", host.ID, "last_seen_at", host.LastSeenAt, "containers", len(live))
			for _, container := range live {
				logger.InfoContext(ctx, "container stopped", "container_id", container, "host_id", host.ID, "reason", StopHostLost)
			}
			delete(containers, host.ID)
		})
}

// timeOutPending stops as start failures the fleet containers pending past
// the queue timeout that no launched host will take, each naming why the
// planner last said it waits.
func (e *Execution) timeOutPending(ctx context.Context, logger *slog.Logger) error {
	timeout := compute.DefaultPolicy().QueueTimeout
	return sweep(ctx, e, logger, "time out pending container", "container_id", func(c StalePendingContainersRow) any { return c.ID },
		func(last *StalePendingContainersRow) ([]StalePendingContainersRow, error) {
			params := StalePendingContainersParams{TimeoutSeconds: timeout.Seconds(), BatchSize: recoveryBatch}
			if last != nil {
				params.AfterCreatedAt, params.AfterID = last.CreatedAt, last.ID
			}
			return e.queries.StalePendingContainers(ctx, params)
		},
		func(tx pgx.Tx, container StalePendingContainersRow) (bool, error) {
			n, err := e.queries.WithTx(tx).LockStalePendingContainer(ctx, LockStalePendingContainerParams{
				ID: container.ID, TimeoutSeconds: timeout.Seconds(),
			})
			if err != nil || n != 1 {
				return false, err
			}
			message := fmt.Sprintf("no capacity within %s: %s", timeout, compute.CapacityWait(container.Wait).Message())
			if compute.CapacityWait(container.Wait) == compute.WaitQuota && container.Connected {
				message += "; raise it in AWS Service Quotas, linked from your AWS connection"
			}
			return true, e.containerExited(ctx, tx, ContainerID(container.ID), ContainerExit{Reason: StopStartFailed, Message: message})
		},
		func(container StalePendingContainersRow) {
			logger.WarnContext(ctx, "container found no capacity", "container_id", container.ID, "wait", container.Wait)
		})
}
