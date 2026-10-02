package main

import (
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// An idle scheduler delivers a callback as soon as one is queued, though
// with no live work its delivery loop would otherwise wait for the safety
// tick.
func TestQueuedCallbacksWakeAnIdleScheduler(t *testing.T) {
	pool := dbtest.New(t)
	schedulerEnv(t, pool)
	t.Setenv("LAZYCLOUD_CALLBACK_ALLOW_PRIVATE", "1")
	delivered := make(chan time.Time, 1)
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		select {
		case delivered <- time.Now():
		default:
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(receiver.Close)
	health := freeAddr(t)
	t.Setenv("LAZYCLOUD_HEALTH_ADDR", health)
	ctx, cancel := context.WithCancel(t.Context())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- run(ctx, slog.New(slog.DiscardHandler)) }()
	waitStatus(t, "http://"+health+"/readyz", http.StatusOK)
	// Past the start-up passes, the delivery loop waits for a wake.
	time.Sleep(2 * time.Second)

	queued := time.Now()
	err := pgx.BeginFunc(t.Context(), pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(t.Context(), `
with ws as (insert into workspaces (name) values ('callbacks') returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id, workspace_id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, jsonb_build_object('callback_url', $1::text), sha256('spec'), sha256('src') from wl returning id, workload_id),
     task as (insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts, finished_at)
              select app.workspace_id, rel.workload_id, rel.id, 'succeeded', 1, 1, now() from app, rel returning id, workspace_id)
insert into task_callbacks (task_id, workspace_id, url, event, attempt, max_attempts)
select id, workspace_id, $1, 'succeeded', 1, 1 from task`, receiver.URL+"/hook"); err != nil {
			return err
		}
		return database.Notify(t.Context(), tx, database.ChannelCallback, "")
	})
	if err != nil {
		t.Fatal(err)
	}
	select {
	case at := <-delivered:
		t.Logf("callback delivered %s after it was queued", at.Sub(queued))
	case <-time.After(3 * time.Second):
		t.Fatal("an idle scheduler did not deliver a queued callback within 3s")
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("run: %v", err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the scheduler did not stop within 10s of shutdown")
	}
}
