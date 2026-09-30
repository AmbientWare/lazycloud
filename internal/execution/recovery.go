package execution

import (
	"context"
	"errors"
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

// TimeOutAttempts ends every running attempt past its deadline as timed out,
// applies the retry policy and wakes the host so it kills the slot. Each
// attempt commits on its own. It returns how many attempts it ended.
func (e *Execution) TimeOutAttempts(ctx context.Context, logger *slog.Logger) (int, error) {
	params := OverdueAttemptsParams{BatchSize: recoveryBatch}
	ended := 0
	for {
		overdue, err := e.queries.OverdueAttempts(ctx, params)
		if err != nil {
			return ended, fmt.Errorf("list overdue attempts: %w", err)
		}
		for _, attempt := range overdue {
			err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
				err := e.finishAttempt(ctx, tx, nil, AttemptOutcome{
					Attempt: AttemptID(attempt.ID),
					State:   AttemptTimedOut,
					Failure: &Failure{Kind: FailureTimeout, Message: "attempt exceeded its timeout"},
				})
				if err != nil {
					return err
				}
				if attempt.HostID == nil {
					return nil
				}
				return database.Notify(ctx, tx, database.ChannelHost, attempt.HostID.String())
			})
			switch {
			case err == nil:
				ended++
				logger.InfoContext(ctx, "attempt timed out", "attempt_id", attempt.ID, "host_id", attempt.HostID)
			case errors.Is(err, ErrStaleAttempt):
				// It finished after the scan.
			case ctx.Err() != nil:
				return ended, fmt.Errorf("time out attempt: %w", err)
			default:
				logger.ErrorContext(ctx, "time out attempt", "attempt_id", attempt.ID, "error", err)
			}
		}
		if len(overdue) < recoveryBatch {
			return ended, nil
		}
		last := overdue[len(overdue)-1]
		params.AfterDeadlineAt, params.AfterID = last.DeadlineAt, last.ID
	}
}

// TimeOutStarts stops containers that stayed starting longer than
// StartTimeout as start failures. It returns how many it stopped.
func (e *Execution) TimeOutStarts(ctx context.Context, logger *slog.Logger) (int, error) {
	params := StuckStartingContainersParams{TimeoutSeconds: StartTimeout.Seconds(), BatchSize: recoveryBatch}
	stopped := 0
	for {
		stuck, err := e.queries.StuckStartingContainers(ctx, params)
		if err != nil {
			return stopped, fmt.Errorf("list stuck containers: %w", err)
		}
		for _, container := range stuck {
			var timedOut bool
			err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
				n, err := e.queries.WithTx(tx).LockStuckStartingContainer(ctx, LockStuckStartingContainerParams{
					ID: container.ID, TimeoutSeconds: StartTimeout.Seconds(),
				})
				if err != nil {
					return fmt.Errorf("lock container: %w", err)
				}
				if timedOut = n == 1; !timedOut {
					return nil
				}
				return e.containerExited(ctx, tx, ContainerID(container.ID), ContainerExit{
					Reason:  StopStartFailed,
					Message: fmt.Sprintf("container did not become ready within %s", StartTimeout),
				})
			})
			switch {
			case err == nil && timedOut:
				stopped++
				logger.WarnContext(ctx, "container start timed out", "container_id", container.ID)
			case err == nil:
			case ctx.Err() != nil:
				return stopped, fmt.Errorf("time out container start: %w", err)
			default:
				logger.ErrorContext(ctx, "time out container start", "container_id", container.ID, "error", err)
			}
		}
		if len(stuck) < recoveryBatch {
			return stopped, nil
		}
		last := stuck[len(stuck)-1]
		// The query filters on assigned_at, so it is never null here.
		params.AfterAssignedAt, params.AfterID = *last.AssignedAt, last.ID
	}
}

// ReleaseLostHosts marks hosts silent for longer than
// compute.LivenessTimeout as lost and stops their live containers as
// host_lost in the same transaction, so their running attempts retry by
// policy. Each host commits on its own. It returns how many hosts it marked.
func (e *Execution) ReleaseLostHosts(ctx context.Context, logger *slog.Logger) (int, error) {
	var after *compute.StaleHost
	lost := 0
	for {
		hosts, err := compute.StaleHosts(ctx, e.pool, after, recoveryBatch)
		if err != nil {
			return lost, fmt.Errorf("release lost host: %w", err)
		}
		for _, host := range hosts {
			var containers []uuid.UUID
			var marked bool
			err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
				var err error
				if marked, err = compute.MarkLost(ctx, tx, host.ID); err != nil || !marked {
					return err
				}
				hostID := uuid.UUID(host.ID)
				if containers, err = e.queries.WithTx(tx).LiveContainersOnHost(ctx, &hostID); err != nil {
					return fmt.Errorf("list live containers: %w", err)
				}
				for _, container := range containers {
					if err := e.containerExited(ctx, tx, ContainerID(container), ContainerExit{
						Reason: StopHostLost, Message: "host stopped reporting",
					}); err != nil {
						return fmt.Errorf("stop container %s: %w", container, err)
					}
				}
				return nil
			})
			switch {
			case err == nil && marked:
				lost++
				logger.WarnContext(ctx, "host lost", "host_id", host.ID, "last_seen_at", host.LastSeenAt, "containers", len(containers))
				for _, container := range containers {
					logger.InfoContext(ctx, "container stopped", "container_id", container, "host_id", host.ID, "reason", StopHostLost)
				}
			case err == nil:
			case ctx.Err() != nil:
				return lost, fmt.Errorf("release lost host: %w", err)
			default:
				logger.ErrorContext(ctx, "release lost host", "host_id", host.ID, "error", err)
			}
		}
		if len(hosts) < recoveryBatch {
			return lost, nil
		}
		after = &hosts[len(hosts)-1]
	}
}
