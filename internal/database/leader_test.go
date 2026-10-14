package database_test

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// candidate runs Lead until stopped and records whether it leads.
type candidate struct {
	leading atomic.Bool
	stop    context.CancelFunc
}

// One of two candidates leads while the other retries without a statement
// open, which would hold back vacuum. A leader whose session is terminated
// steps down and the other leads; the lost one tries again and takes over
// when that one stops.
func TestLeadElectsOneAndHandsOver(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	const check, retry = 20 * time.Millisecond, 20 * time.Millisecond
	var wg sync.WaitGroup
	defer wg.Wait()
	candidates := make([]*candidate, 2)
	for n := range candidates {
		ctx, cancel := context.WithCancel(t.Context())
		c := &candidate{stop: cancel}
		candidates[n] = c
		wg.Go(func() {
			if err := database.Lead(ctx, pool, "test_leader", check, retry, c.leading.Store, logger); err != nil {
				t.Error(err)
			}
		})
		defer cancel()
	}
	leaders := func() []int {
		var out []int
		for n, c := range candidates {
			if c.leading.Load() {
				out = append(out, n)
			}
		}
		return out
	}
	// waitFor polls until done holds.
	waitFor := func(step string, done func() bool) {
		t.Helper()
		for deadline := time.Now().Add(5 * time.Second); !done(); time.Sleep(5 * time.Millisecond) {
			if time.Now().After(deadline) {
				t.Fatalf("%s: leaders %v", step, leaders())
			}
		}
	}

	waitFor("start", func() bool { return len(leaders()) == 1 })
	first := leaders()[0]
	var elected time.Time
	if err := pool.QueryRow(t.Context(), "select clock_timestamp()").Scan(&elected); err != nil {
		t.Fatal(err)
	}
	// The standby tried again after the election and sits idle between
	// tries, holding no snapshot.
	waitFor("standby retries", func() bool {
		if l := leaders(); len(l) != 1 || l[0] != first {
			t.Fatalf("leaders %v while %d holds the lock", l, first)
		}
		var idle int
		if err := pool.QueryRow(t.Context(), `select count(*) from pg_stat_activity
			where datname = current_database() and state = 'idle' and backend_xmin is null
			  and query like 'select pg_try_advisory_lock%' and query_start > $1`, elected).Scan(&idle); err != nil {
			t.Fatal(err)
		}
		return idle == 1
	})

	// Ending the leader's session releases the lock; the leader notices
	// within its check. Only this test's database: other packages' tests
	// share the server.
	if _, err := pool.Exec(t.Context(), `select pg_terminate_backend(l.pid) from pg_locks l
		join pg_database d on d.oid = l.database
		where l.locktype = 'advisory' and l.granted and l.pid <> pg_backend_pid()
		  and d.datname = current_database()`); err != nil {
		t.Fatal(err)
	}
	second := 1 - first
	waitFor("after the session ended", func() bool { l := leaders(); return len(l) == 1 && l[0] == second })

	candidates[second].stop()
	waitFor("after the leader stopped", func() bool { l := leaders(); return len(l) == 1 && l[0] == first })
}

// The leader's check finds its own session's lock and nothing else: not
// another session's hold, not another name, not a lock it released. Names
// and markers cover both signs of the 64-bit key, whose high half pg_locks
// keeps apart.
func TestLeaderCheckSeesOnlyItsOwnSessionsLock(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	pool := dbtest.New(t)
	acquire := func() *pgxpool.Conn {
		t.Helper()
		conn, err := pool.Acquire(ctx)
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(conn.Release)
		return conn
	}
	holds := func(conn *pgxpool.Conn, name string, marker int64) bool {
		t.Helper()
		held, err := database.HoldsLead(ctx, conn.Conn(), name, marker)
		if err != nil {
			t.Fatal(err)
		}
		return held
	}
	leader, other := acquire(), acquire()
	const leaderMarker, otherMarker = int64(-7_000_000_000_000_000_001), int64(42)
	for conn, marker := range map[*pgxpool.Conn]int64{leader: leaderMarker, other: otherMarker} {
		if _, err := conn.Exec(ctx, "select pg_advisory_lock($1)", marker); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range []string{"scheduler_leader", "test_leader"} {
		var key int64
		if err := leader.QueryRow(ctx, "select hashtextextended($1, 0)", name).Scan(&key); err != nil {
			t.Fatal(err)
		}
		if _, err := leader.Exec(ctx, "select pg_advisory_lock(hashtextextended($1, 0))", name); err != nil {
			t.Fatal(err)
		}
		if !holds(leader, name, leaderMarker) || holds(other, name, otherMarker) || holds(leader, name+"_other", leaderMarker) {
			t.Fatalf("%s (key %d): the check must see the lock in the holding session only", name, key)
		}
		if _, err := leader.Exec(ctx, "select pg_advisory_unlock(hashtextextended($1, 0))", name); err != nil {
			t.Fatal(err)
		}
		if holds(leader, name, leaderMarker) {
			t.Fatalf("%s: the check sees a released lock", name)
		}
	}
}
