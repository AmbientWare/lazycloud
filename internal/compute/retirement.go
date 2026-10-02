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

const (
	// serviceLostAfter is how long a ready cloud host may stay lost before
	// its instance is replaced.
	serviceLostAfter = 5 * time.Minute
	// launchGrace is how long EC2 may take to list a launched instance.
	launchGrace = 2 * time.Minute
)

// RetireResult summarizes one retirement pass.
type RetireResult struct {
	Drained    int
	Terminated int
}

// Retire drains a disconnecting account's hosts and terminates draining
// hosts that run nothing. Which idle hosts leave is the planning pass's
// decision (Plan).
func (c *Compute) Retire(ctx context.Context, logger *slog.Logger) (RetireResult, error) {
	var result RetireResult
	var terminate []ClaimTerminationsRow
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		result, terminate = RetireResult{}, nil
		q := c.queries.WithTx(tx)
		disconnecting, err := q.DrainConnectionHosts(ctx)
		if err != nil {
			return fmt.Errorf("drain disconnecting hosts: %w", err)
		}
		for _, h := range disconnecting {
			if err := c.containers.DrainHostContainers(ctx, tx, HostID(h)); err != nil {
				return err
			}
		}
		result.Drained += len(disconnecting)
		if err := q.CancelConnectionLaunches(ctx); err != nil {
			return fmt.Errorf("cancel launches: %w", err)
		}
		if terminate, err = q.ClaimTerminations(ctx); err != nil {
			return fmt.Errorf("claim terminations: %w", err)
		}
		return nil
	})
	if err != nil {
		return RetireResult{}, fmt.Errorf("retire hosts: %w", err)
	}
	for _, h := range terminate {
		scope, err := c.scopeOf(ctx, h.ConnectionID)
		if err == nil {
			err = c.terminate(ctx, scope, h.Region, deref(h.InstanceID))
		}
		if err != nil {
			// Reconciliation terminates it again.
			logger.ErrorContext(ctx, "terminate host", "host_id", h.ID, "error", err)
			continue
		}
		result.Terminated++
		logger.InfoContext(ctx, "host terminating", "host_id", h.ID, "instance_id", deref(h.InstanceID))
	}
	return result, nil
}

// scopeOf is the credentials scope of the platform or a connection.
func (c *Compute) scopeOf(ctx context.Context, connection *uuid.UUID) (awsScope, error) {
	if connection == nil {
		return awsScope{key: string(KindPlatform)}, nil
	}
	row, err := c.queries.ConnectionScope(ctx, *connection)
	if errors.Is(err, pgx.ErrNoRows) {
		return awsScope{}, errors.New("the connection has no active authorization")
	}
	if err != nil {
		return awsScope{}, fmt.Errorf("read connection scope: %w", err)
	}
	return c.aws().assume(row.RoleArn, row.ExternalID, "lazycloud-fleet-"+connection.String()[:8], connection.String()), nil
}
