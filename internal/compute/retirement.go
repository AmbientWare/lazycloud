package compute

import (
	"context"
	"fmt"
	"log/slog"

	"github.com/jackc/pgx/v5"
)

// Retire drains a disconnecting account's hosts and removes its requested
// ones. Which idle hosts leave is the planning pass's decision (Plan); the
// actuator terminates a drained host once it runs nothing.
func (c *Compute) Retire(ctx context.Context, _ *slog.Logger) error {
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
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
		if err := q.CancelConnectionLaunches(ctx); err != nil {
			return fmt.Errorf("cancel launches: %w", err)
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("retire hosts: %w", err)
	}
	return nil
}
