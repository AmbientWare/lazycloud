package compute

import (
	"context"
	"fmt"
	"log/slog"

	"github.com/jackc/pgx/v5"
)

// RetireResult summarizes one retirement pass.
type RetireResult struct {
	Drained int
}

// Retire drains a disconnecting account's hosts and removes its requested
// ones. Which idle hosts leave is the planning pass's decision (Plan); the
// actuator terminates a drained host once it runs nothing.
func (c *Compute) Retire(ctx context.Context, _ *slog.Logger) (RetireResult, error) {
	var result RetireResult
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		result = RetireResult{}
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
		return nil
	})
	if err != nil {
		return RetireResult{}, fmt.Errorf("retire hosts: %w", err)
	}
	return result, nil
}
