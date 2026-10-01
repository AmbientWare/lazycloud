package observability_test

import (
	"errors"
	"log/slog"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

// One caller may hold MaxPerPrincipal streams; closing one frees its place.
func TestStreamsPerCallerAreCapped(t *testing.T) {
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
	f := newFixture(t, `{"max_pending_tasks": 100}`)
	cfg := smallHub()
	cfg.Idle = 50 * time.Millisecond
	_, sub := runHub(t, f.pool, cfg, f.workspace)
	time.Sleep(300 * time.Millisecond)
	f.submit(1)
	nextEvent(t, sub)
	if reason := sub.TakeReset(); reason != "" {
		t.Fatalf("idle checks reset the stream: %s", reason)
	}

	f.exec1(`select pg_terminate_backend(pid) from pg_stat_activity
where datname = current_database() and pid <> pg_backend_pid() and (query = 'select 1' or query like 'listen%')`)
	select {
	case <-sub.Reset():
	case <-time.After(10 * time.Second):
		t.Fatal("a lost listener did not reset the stream")
	}
	if reason := sub.TakeReset(); reason != observability.ResetMissed {
		t.Fatalf("reset reason %q", reason)
	}
	deadline := time.Now().Add(15 * time.Second)
	for {
		f.submit(1)
		select {
		case <-sub.Events():
			return
		case <-time.After(500 * time.Millisecond):
		}
		sub.TakeReset()
		if time.Now().After(deadline) {
			t.Fatal("no change after the hub reconnected")
		}
	}
}
