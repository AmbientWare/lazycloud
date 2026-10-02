package database

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"io/fs"
	"sort"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/migrations"
)

// migrationLock serializes migration runs across processes starting together.
const migrationLock = 7_302_114_001

// ErrMigrationChanged means a deployed migration file no longer matches the
// digest recorded when it was applied. Deployed revisions are frozen.
var ErrMigrationChanged = errors.New("applied migration changed")

// Migrate applies every migration in migrations.Files that has not run yet,
// each in its own transaction.
func Migrate(ctx context.Context, pool *pgxpool.Pool) error {
	conn, err := pool.Acquire(ctx)
	if err != nil {
		return fmt.Errorf("acquire migration connection: %w", err)
	}
	defer conn.Release()

	if _, err := conn.Exec(ctx, "select pg_advisory_lock($1)", migrationLock); err != nil {
		return fmt.Errorf("lock migrations: %w", err)
	}
	defer func() {
		// The lock is session-scoped; a failed unlock ends with the connection.
		_, _ = conn.Exec(context.WithoutCancel(ctx), "select pg_advisory_unlock($1)", migrationLock)
	}()

	if _, err := conn.Exec(ctx, `create table if not exists schema_migrations (
		name text primary key,
		sha256 bytea not null,
		applied_at timestamptz not null default now()
	)`); err != nil {
		return fmt.Errorf("create schema_migrations: %w", err)
	}

	applied := map[string][]byte{}
	rows, err := conn.Query(ctx, "select name, sha256 from schema_migrations")
	if err != nil {
		return fmt.Errorf("read applied migrations: %w", err)
	}
	for rows.Next() {
		var name string
		var digest []byte
		if err := rows.Scan(&name, &digest); err != nil {
			rows.Close()
			return fmt.Errorf("scan applied migration: %w", err)
		}
		applied[name] = digest
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("read applied migrations: %w", err)
	}

	names, err := fs.Glob(migrations.Files, "*.sql")
	if err != nil {
		return fmt.Errorf("list migrations: %w", err)
	}
	sort.Strings(names)
	for _, name := range names {
		body, err := fs.ReadFile(migrations.Files, name)
		if err != nil {
			return fmt.Errorf("read migration %s: %w", name, err)
		}
		digest := sha256.Sum256(body)
		if recorded, ok := applied[name]; ok {
			if string(recorded) != string(digest[:]) {
				return fmt.Errorf("%w: %s", ErrMigrationChanged, name)
			}
			continue
		}
		err = pgx.BeginFunc(ctx, conn, func(tx pgx.Tx) error {
			if _, err := tx.Exec(ctx, string(body)); err != nil {
				return fmt.Errorf("apply: %w", err)
			}
			if _, err := tx.Exec(ctx, "insert into schema_migrations (name, sha256) values ($1, $2)", name, digest[:]); err != nil {
				return fmt.Errorf("record: %w", err)
			}
			return nil
		})
		if err != nil {
			return fmt.Errorf("migration %s: %w", name, err)
		}
	}
	return nil
}
