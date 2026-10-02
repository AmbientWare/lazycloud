package database

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// leadRetry paces new attempts after the leader connection fails.
const leadRetry = time.Second

// Lead elects one leader among the processes that call it with the same
// name, until ctx ends. It holds a session advisory lock on a connection
// taken out of the pool: a process waiting for the lock blocks inside
// PostgreSQL and sends nothing, and the lock is released when the leader's
// session ends. leading is called with true once the lock is held and with
// false when the session is lost; a leader pings its session every check, so
// it learns of a lost session within that long. Another process may lead
// before a lost leader notices, so leadership only paces work whose passes
// are already safe to overlap.
func Lead(ctx context.Context, pool *pgxpool.Pool, name string, check time.Duration, leading func(bool), logger *slog.Logger) error {
	for {
		err := holdLead(ctx, pool, name, check, leading)
		if ctx.Err() != nil {
			return nil //nolint:nilerr // Cancellation is the normal stop.
		}
		logger.WarnContext(ctx, "leadership lost", "name", name, "error", err)
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(leadRetry):
		}
	}
}

func holdLead(ctx context.Context, pool *pgxpool.Pool, name string, check time.Duration, leading func(bool)) error {
	pooled, err := pool.Acquire(ctx)
	if err != nil {
		return fmt.Errorf("acquire leader connection: %w", err)
	}
	// The lock belongs to this session, so the connection never returns to
	// the pool; closing it gives the lock up.
	conn := pooled.Hijack()
	defer conn.Close(context.WithoutCancel(ctx)) //nolint:errcheck // Closing a dead leader connection has no recovery.
	if _, err := conn.Exec(ctx, "select pg_advisory_lock(hashtextextended($1, 0))", name); err != nil {
		return fmt.Errorf("wait for leadership: %w", err)
	}
	leading(true)
	defer leading(false)
	ticker := time.NewTicker(check)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			if err := conn.Ping(ctx); err != nil {
				return fmt.Errorf("leader session: %w", err)
			}
		}
	}
}
