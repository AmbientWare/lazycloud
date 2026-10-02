package database

import (
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

// leadRetry paces new attempts after the leader connection fails.
const leadRetry = time.Second

// lockNotAvailable is SQLSTATE 55P03, which ends a lock wait that passed
// lock_timeout.
const lockNotAvailable = "55P03"

// errStillStandby ends a wait for the lock that timed out while another
// session leads.
var errStillStandby = errors.New("another session leads")

// Lead elects one leader among the processes that call it with the same
// name, until ctx ends. It holds a session advisory lock on a connection
// taken out of the pool: a process waiting for the lock blocks inside
// PostgreSQL and sends nothing, and the lock is released when the leader's
// session ends. pool must keep a session on one backend
// (database.OpenSession). leading is called with true once the lock is held
// and with false when it is lost; every check the leader asks PostgreSQL
// whether its session still holds the lock, so it learns within that long
// of a session that ended or that a proxy moved to another backend, where a
// ping would still succeed. Another process may lead before a lost leader
// notices, so leadership only paces work whose passes are already safe to
// overlap.
//
// A router such as Neki's answers pg_backend_pid() itself, so the check
// cannot match pg_locks against it. The leader instead also locks a random
// key only it knows, and holds the lead while one backend holds both locks.
// Neki also ends a lock wait after its lock_timeout; a standby then simply
// waits again.
func Lead(ctx context.Context, pool *pgxpool.Pool, name string, check time.Duration, leading func(bool), logger *slog.Logger) error {
	for {
		err := holdLead(ctx, pool, name, check, leading)
		if ctx.Err() != nil {
			return nil //nolint:nilerr // Cancellation is the normal stop.
		}
		if !errors.Is(err, errStillStandby) {
			logger.WarnContext(ctx, "leadership lost", "name", name, "error", err)
		}
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
		if pgErr, ok := errors.AsType[*pgconn.PgError](err); ok && pgErr.Code == lockNotAvailable {
			return errStillStandby
		}
		return fmt.Errorf("wait for leadership: %w", err)
	}
	var mark [8]byte
	if _, err := rand.Read(mark[:]); err != nil {
		return fmt.Errorf("leader session marker: %w", err)
	}
	marker := int64(binary.BigEndian.Uint64(mark[:])) //nolint:gosec // Any 64 bits make a key.
	if _, err := conn.Exec(ctx, "select pg_advisory_lock($1)", marker); err != nil {
		return fmt.Errorf("mark leader session: %w", err)
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
			held, err := holdsLead(ctx, conn, name, marker)
			if err != nil {
				return fmt.Errorf("leader session: %w", err)
			}
			if !held {
				return errors.New("leader session no longer holds the lock")
			}
		}
	}
}

// holdsLead reports whether the backend holding the advisory lock of name
// also holds marker, the key only the leader's session locked. pg_locks
// splits a 64-bit key into classid (high half) and objid (low half), with
// objsubid 1.
func holdsLead(ctx context.Context, conn *pgx.Conn, name string, marker int64) (bool, error) {
	var held bool
	err := conn.QueryRow(ctx, `select exists (select 1 from pg_locks l join pg_locks m on m.pid = l.pid
		where l.locktype = 'advisory' and l.granted and l.objsubid = 1
		  and (l.classid::bigint << 32 | l.objid::bigint) = hashtextextended($1, 0)
		  and m.locktype = 'advisory' and m.granted and m.objsubid = 1
		  and (m.classid::bigint << 32 | m.objid::bigint) = $2)`, name, marker).Scan(&held)
	if err != nil {
		return false, fmt.Errorf("check leader lock: %w", err)
	}
	return held, nil
}
