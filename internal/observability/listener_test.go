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
	cfg := observability.DefaultChangesConfig()
	cfg.MaxPerPrincipal = 2
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

// A hub's bounds and intervals are all set: none of them stands for
// unlimited or a default when zero.
func TestChangesConfigNeedsEveryField(t *testing.T) {
	t.Parallel()
	if err := observability.DefaultChangesConfig().Validate(); err != nil {
		t.Fatalf("the default: %v", err)
	}
	for name, unset := range map[string]func(*observability.ChangesConfig){
		"retained":   func(c *observability.ChangesConfig) { c.Retained = 0 },
		"buffer":     func(c *observability.ChangesConfig) { c.Buffer = 0 },
		"per server": func(c *observability.ChangesConfig) { c.MaxSubscribers = 0 },
		"per caller": func(c *observability.ChangesConfig) { c.MaxPerPrincipal = 0 },
		"idle":       func(c *observability.ChangesConfig) { c.Idle = 0 },
		"reconnect":  func(c *observability.ChangesConfig) { c.Reconnect = 0 },
		"negative":   func(c *observability.ChangesConfig) { c.Reconnect = -time.Second },
	} {
		cfg := observability.DefaultChangesConfig()
		unset(&cfg)
		if err := cfg.Validate(); err == nil {
			t.Errorf("%s: accepted %+v", name, cfg)
		}
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
