package compute

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// PreemptLead is how long before the provider reclaims an interrupted host
// that its remaining containers stop, so their attempts retry elsewhere
// while the host still answers.
const PreemptLead = 20 * time.Second

// ReclaimGrace is how long past its reclaim time an interrupted host is
// left for the provider. EC2 charges nothing for a Spot instance it
// reclaims within its first hour but bills one the fleet terminates, so the
// fleet terminates an interrupted host only once the provider has not.
const ReclaimGrace = 2 * time.Minute

// ReportInterruption records a provider's notice that it reclaims host at
// reclaimAt. In one transaction the host stops taking work (draining,
// preempting) and its containers drain through execution: they claim
// nothing more and stop as their running attempts finish. A repeated notice
// changes nothing.
func (c *Compute) ReportInterruption(ctx context.Context, host HostID, reason string, reclaimAt time.Time) error {
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, err := q.LockHost(ctx, uuid.UUID(host))
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrUnknownHost
		}
		if err != nil {
			return fmt.Errorf("lock host: %w", err)
		}
		if row.InterruptionAt != nil {
			return nil
		}
		message := fmt.Sprintf("The provider reclaims this instance at %s", reclaimAt.UTC().Format(time.RFC3339))
		if err := q.MarkInterrupted(ctx, MarkInterruptedParams{
			ID: row.ID, Message: message, Reason: reason, ReclaimAt: &reclaimAt,
		}); err != nil {
			return fmt.Errorf("mark interrupted: %w", err)
		}
		if err := c.containers.DrainHostContainers(ctx, tx, host); err != nil {
			return err
		}
		return notifyMachines(ctx, tx, row.ID)
	})
	if err != nil {
		return fmt.Errorf("report interruption: %w", err)
	}
	return nil
}

// Preempt stops what still runs on interrupted hosts within PreemptLead of
// their reclaim time, through execution, so running attempts are preempted
// and retried. The host stays draining until the provider reclaims it. Each
// host commits on its own. It returns how many hosts it preempted.
func (c *Compute) Preempt(ctx context.Context, logger *slog.Logger) (int, error) {
	due, err := c.queries.DuePreemptions(ctx, DuePreemptionsParams{LeadSeconds: PreemptLead.Seconds(), BatchSize: 100})
	if err != nil {
		return 0, fmt.Errorf("list due preemptions: %w", err)
	}
	preempted := 0
	for _, id := range due {
		var stopped int
		err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
			var err error
			if stopped, err = c.containers.StopHostContainers(ctx, tx, HostID(id), "the host was reclaimed"); err != nil {
				return err
			}
			return notifyMachines(ctx, tx, id)
		})
		if err != nil {
			if ctx.Err() != nil {
				return preempted, fmt.Errorf("preempt host: %w", err)
			}
			logger.ErrorContext(ctx, "preempt host", "host_id", id, "error", err)
			continue
		}
		preempted++
		logger.InfoContext(ctx, "host preempted", "host_id", id, "containers", stopped)
	}
	return preempted, nil
}
