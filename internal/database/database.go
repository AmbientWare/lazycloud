// Package database opens PostgreSQL pools, applies the migration chain and
// fans out NOTIFY wake-ups. PostgreSQL is the durable authority; owners keep
// their own queries.
package database

import (
	"context"
	"fmt"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Open connects a pool and checks the server is reachable.
func Open(ctx context.Context, url string) (*pgxpool.Pool, error) {
	config, err := pgxpool.ParseConfig(url)
	if err != nil {
		return nil, fmt.Errorf("parse database url: %w", err)
	}
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return nil, fmt.Errorf("open database pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("ping database: %w", err)
	}
	return pool, nil
}

// OpenSession returns the pool of connections that keep state in their
// session: LISTEN, session advisory locks and the migration lock. A
// transaction pooler runs a session's later statements on other backends,
// where that state is gone, so these connect through url, a direct
// connection; main serves everything else. An empty url uses main.
// closePool releases the pool it opened.
func OpenSession(ctx context.Context, url string, main *pgxpool.Pool) (pool *pgxpool.Pool, closePool func(), err error) {
	if url == "" {
		return main, func() {}, nil
	}
	if pool, err = Open(ctx, url); err != nil {
		return nil, nil, fmt.Errorf("session pool: %w", err)
	}
	return pool, pool.Close, nil
}

// Notify queues a wake-up that PostgreSQL delivers when tx commits.
func Notify(ctx context.Context, tx pgx.Tx, channel Channel, payload string) error {
	if _, err := tx.Exec(ctx, "select pg_notify($1, $2)", string(channel), payload); err != nil {
		return fmt.Errorf("notify %s: %w", channel, err)
	}
	return nil
}

// PostgreSQL rejects a NOTIFY payload of 8000 bytes or more.
const maxPayloadBytes = 7999

// NotifyAll queues a wake-up for each distinct payload in one statement,
// packed into as few notifications as maxPayloadBytes allows. Every
// notifying commit writes its notifications under one shared lock, so
// fewer notifications hold it for less time.
func NotifyAll(ctx context.Context, tx pgx.Tx, channel Channel, payloads []string) error {
	if len(payloads) == 0 {
		return nil
	}
	seen := make(map[string]bool, len(payloads))
	var packed []string
	var b strings.Builder
	for _, p := range payloads {
		if seen[p] {
			continue
		}
		seen[p] = true
		if b.Len() > 0 && b.Len()+len(payloadSeparator)+len(p) > maxPayloadBytes {
			packed = append(packed, b.String())
			b.Reset()
		}
		if b.Len() > 0 {
			b.WriteString(payloadSeparator)
		}
		b.WriteString(p)
	}
	packed = append(packed, b.String())
	if _, err := tx.Exec(ctx, "select pg_notify($1, p) from unnest($2::text[]) p", string(channel), packed); err != nil {
		return fmt.Errorf("notify %s: %w", channel, err)
	}
	return nil
}
