package agent

import (
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/google/uuid"
)

// fillState writes bytes of data into state, as a build would.
func fillState(t *testing.T, state string, bytes int) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(state, uuid.NewString()), make([]byte, bytes), 0o600); err != nil {
		t.Fatal(err)
	}
}

// Builds of one workspace that run at once each hold a state of their own,
// and a state is held again once its build ends.
func TestBuildCachesGiveConcurrentBuildsTheirOwnState(t *testing.T) {
	caches := newBuildCaches(t.TempDir(), 1<<30, slog.Default())
	workspace := uuid.NewString()
	first, err := caches.acquire(t.Context(), workspace)
	if err != nil {
		t.Fatal(err)
	}
	second, err := caches.acquire(t.Context(), workspace)
	if err != nil {
		t.Fatal(err)
	}
	if first == second {
		t.Fatalf("two running builds share the state %s", first)
	}
	caches.release(t.Context(), first)
	again, err := caches.acquire(t.Context(), workspace)
	if err != nil {
		t.Fatal(err)
	}
	if again != first {
		t.Fatalf("a build after an ended one holds %s, want its state %s", again, first)
	}
	if _, err := caches.acquire(t.Context(), "../other"); err == nil {
		t.Fatal("a build that names no workspace id got a state")
	}
}

// A build that names no workspace gets an empty state no other build holds,
// and the state goes once its build ends.
func TestBuildCachesGiveUnscopedBuildsAnEmptyStateOfTheirOwn(t *testing.T) {
	caches := newBuildCaches(t.TempDir(), 1<<30, slog.Default())
	first, err := caches.acquire(t.Context(), "")
	if err != nil {
		t.Fatal(err)
	}
	fillState(t, first, 1<<10)
	caches.release(t.Context(), first)
	second, err := caches.acquire(t.Context(), "")
	if err != nil {
		t.Fatal(err)
	}
	if entries, err := os.ReadDir(second); err != nil || len(entries) != 0 || second == first {
		t.Fatalf("an unscoped build got %s holding %d entries (%v), want a new empty state", second, len(entries), err)
	}
	caches.evict(t.Context())
	if _, err := os.Stat(first); err == nil {
		t.Fatal("an ended unscoped build's state stayed")
	}
	if _, err := os.Stat(second); err != nil {
		t.Fatalf("a running unscoped build's state was removed: %v", err)
	}
}

// The caches stay within their limit: when a build ends, the least recently
// used workspaces are removed whole, and never one whose build still runs.
func TestBuildCachesEvictLeastRecentlyUsedWorkspacesToTheLimit(t *testing.T) {
	const limit = 1 << 20
	caches := newBuildCaches(t.TempDir(), limit, slog.Default())
	use := func(workspace string, bytes int) string {
		t.Helper()
		state, err := caches.acquire(t.Context(), workspace)
		if err != nil {
			t.Fatal(err)
		}
		fillState(t, state, bytes)
		return state
	}
	oldest, running, recent := uuid.NewString(), uuid.NewString(), uuid.NewString()
	release := func(state string) {
		caches.release(t.Context(), state)
		caches.evict(t.Context())
	}
	release(use(oldest, 500<<10))
	held := use(running, 500<<10)
	time.Sleep(10 * time.Millisecond)
	release(use(recent, 300<<10))
	if _, err := os.Stat(filepath.Join(caches.dir, oldest)); err != nil {
		t.Fatalf("a cache was removed while the caches fit: %v", err)
	}
	kept := func(want map[string]bool) {
		t.Helper()
		for workspace, keep := range want {
			if _, err := os.Stat(filepath.Join(caches.dir, workspace)); (err == nil) != keep {
				t.Fatalf("workspace %s kept=%v, want %v", workspace, err == nil, keep)
			}
		}
	}
	// Past the limit, the oldest goes and the running build's stays.
	time.Sleep(10 * time.Millisecond)
	release(use(recent, 300<<10))
	kept(map[string]bool{oldest: false, running: true, recent: true})
	// The build that ran ends last, so the other workspace is now the oldest.
	time.Sleep(10 * time.Millisecond)
	release(held)
	kept(map[string]bool{running: true, recent: false})
	_, total, err := caches.measure()
	if err != nil {
		t.Fatal(err)
	}
	if total > limit {
		t.Fatalf("the caches hold %d bytes, more than the limit %d", total, limit)
	}
}
