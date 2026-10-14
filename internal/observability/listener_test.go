package observability_test

import (
	"errors"
	"log/slog"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

// One caller may hold MaxPerPrincipal streams; closing one frees its place.
func TestStreamsPerCallerAreCapped(t *testing.T) {
	t.Parallel()
	cfg := observability.ChangesConfig{Retained: 8, Buffer: 8, MaxSubscribers: 10, MaxPerPrincipal: 2}
	hub := observability.NewChanges(nil, cfg, nil, slog.New(slog.DiscardHandler))
	ws := identity.WorkspaceID(uuid.New())
	first, _, err := hub.Subscribe(ws, "user:a", nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := hub.Subscribe(ws, "user:a", nil); err != nil {
		t.Fatal(err)
	}
	if _, _, err := hub.Subscribe(ws, "user:a", nil); !errors.Is(err, observability.ErrTooManyStreams) {
		t.Fatalf("third stream of one caller: %v", err)
	}
	if _, _, err := hub.Subscribe(ws, "user:b", nil); err != nil {
		t.Fatalf("another caller: %v", err)
	}
	first.Close()
	if _, _, err := hub.Subscribe(ws, "user:a", nil); err != nil {
		t.Fatalf("after a close: %v", err)
	}
}

// An idle listener checks its connection without losing it; a lost
// connection resets every stream, and changes flow again after the hub
// reconnects.
func TestChangeListenerSurvivesIdleAndRecoversFromALostConnection(t *testing.T) {
	t.Parallel()
	f := newFixture(t, `{"max_pending_tasks": 100}`)
	cfg := smallHub()
	cfg.Idle = 50 * time.Millisecond
	_, sub := runHub(t, f.pool, cfg, f.workspace)
	// listener waits for the hub's connection, other than not, to have
	// last run a statement like query and be idle, and returns its pid.
	listener := func(query string, not int) int {
		t.Helper()
		for deadline := time.Now().Add(10 * time.Second); ; {
			var pid int
			err := f.pool.QueryRow(t.Context(), `select pid from pg_stat_activity
				where datname = current_database() and pid not in (pg_backend_pid(), $2) and query like $1 and state = 'idle'`, query, not).Scan(&pid)
			if err == nil {
				return pid
			}
			if !errors.Is(err, pgx.ErrNoRows) {
				t.Fatal(err)
			}
			if time.Now().After(deadline) {
				t.Fatalf("no idle listener connection that last ran %q", query)
			}
			time.Sleep(5 * time.Millisecond)
		}
	}
	// The connection checks itself once idle.
	pid := listener("select 1", 0)
	f.submit(1)
	nextEvent(t, sub)
	if reason := sub.TakeReset(); reason != "" {
		t.Fatalf("idle checks reset the stream: %s", reason)
	}

	f.exec1("select pg_terminate_backend($1)", pid)
	select {
	case <-sub.Reset():
	case <-time.After(10 * time.Second):
		t.Fatal("a lost listener did not reset the stream")
	}
	if reason := sub.TakeReset(); reason != observability.ResetMissed {
		t.Fatalf("reset reason %q", reason)
	}
	// Changes committed once the hub listens again reach the stream.
	listener("listen %", pid)
	f.submit(1)
	nextEvent(t, sub)
}
