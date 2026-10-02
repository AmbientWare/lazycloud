package database_test

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// candidate runs Lead until stopped and records whether it leads.
type candidate struct {
	leading atomic.Bool
	stop    context.CancelFunc
}

// One of two candidates leads; a leader whose session is terminated steps
// down and someone leads again, and when the leader stops the other takes
// over.
func TestLeadElectsOneAndHandsOver(t *testing.T) {
	pool := dbtest.New(t)
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	var wg sync.WaitGroup
	defer wg.Wait()
	candidates := make([]*candidate, 2)
	for n := range candidates {
		ctx, cancel := context.WithCancel(t.Context())
		c := &candidate{stop: cancel}
		candidates[n] = c
		wg.Go(func() {
			if err := database.Lead(ctx, pool, "test_leader", 100*time.Millisecond, c.leading.Store, logger); err != nil {
				t.Error(err)
			}
		})
		defer cancel()
	}
	// leader waits for exactly one candidate to lead and returns its index.
	leader := func(step string) int {
		t.Helper()
		for deadline := time.Now().Add(5 * time.Second); time.Now().Before(deadline); time.Sleep(20 * time.Millisecond) {
			a, b := candidates[0].leading.Load(), candidates[1].leading.Load()
			if a && b {
				t.Fatalf("%s: both candidates lead", step)
			}
			if a != b {
				if a {
					return 0
				}
				return 1
			}
		}
		t.Fatalf("%s: no candidate leads", step)
		return -1
	}

	first := leader("start")
	// The standby stays one while the leader holds the lock.
	time.Sleep(300 * time.Millisecond)
	leader("while held")

	// Ending the leader's session releases the lock; the leader notices
	// within its check.
	// Only this test's database: other packages' tests share the server.
	if _, err := pool.Exec(t.Context(), `select pg_terminate_backend(l.pid) from pg_locks l
		join pg_database d on d.oid = l.database
		where l.locktype = 'advisory' and l.granted and l.pid <> pg_backend_pid()
		  and d.datname = current_database()`); err != nil {
		t.Fatal(err)
	}
	for deadline := time.Now().Add(5 * time.Second); candidates[first].leading.Load() && time.Now().Before(deadline); {
		time.Sleep(20 * time.Millisecond)
	}
	current := leader("after the session ended")

	candidates[current].stop()
	other := 1 - current
	for deadline := time.Now().Add(5 * time.Second); !candidates[other].leading.Load() && time.Now().Before(deadline); {
		time.Sleep(20 * time.Millisecond)
	}
	if !candidates[other].leading.Load() {
		t.Fatal("the standby did not take over after the leader stopped")
	}
}
