package callbacks

import (
	"crypto/rand"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/secrets"
)

type received struct {
	header http.Header
	body   []byte
}

// target answers callbacks with the given statuses in turn, then 204.
type target struct {
	mu       sync.Mutex
	statuses []int
	got      []received
}

func (s *target) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	body, _ := io.ReadAll(r.Body)
	s.mu.Lock()
	s.got = append(s.got, received{header: r.Header.Clone(), body: body})
	status := http.StatusNoContent
	if len(s.statuses) > 0 {
		status, s.statuses = s.statuses[0], s.statuses[1:]
	}
	s.mu.Unlock()
	w.WriteHeader(status)
}

func (s *target) requests() []received {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]received(nil), s.got...)
}

func newSecrets(t *testing.T, pool *pgxpool.Pool) *secrets.Secrets {
	t.Helper()
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		t.Fatal(err)
	}
	key, err := secrets.NewFileKey(raw)
	if err != nil {
		t.Fatal(err)
	}
	return secrets.NewSecrets(pool, key)
}

// callback inserts a succeeded task with a JSON result and its callback row
// for url, and returns the workspace and task.
func callback(t *testing.T, pool *pgxpool.Pool, url string) (identity.WorkspaceID, uuid.UUID) {
	t.Helper()
	var ws, task uuid.UUID
	err := pool.QueryRow(t.Context(), `
with ws as (insert into workspaces (name) values ('ws') returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, '{}'::jsonb, sha256('spec'), sha256('src') from wl returning id, workload_id),
     task as (insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts, finished_at)
              select ws.id, rel.workload_id, rel.id, 'succeeded', 1, 3, now() from ws, rel returning id),
     result as (insert into task_results (task_id, encoding, data) select id, 'json', '{"total": 3}' from task),
     cb as (insert into task_callbacks (task_id, workspace_id, url, event, attempt, max_attempts)
            select task.id, ws.id, $1, 'succeeded', 1, 3 from task, ws)
select ws.id, task.id from ws, task`, url).Scan(&ws, &task)
	if err != nil {
		t.Fatal(err)
	}
	return identity.WorkspaceID(ws), task
}

func state(t *testing.T, pool *pgxpool.Pool) (string, int) {
	t.Helper()
	var s string
	var n int
	if err := pool.QueryRow(t.Context(), "select state, deliveries from task_callbacks").Scan(&s, &n); err != nil {
		t.Fatal(err)
	}
	return s, n
}

// deliverUntil runs passes until the callback leaves pending, as the
// scheduler loop does each tick.
func deliverUntil(t *testing.T, c *Callbacks, pool *pgxpool.Pool) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := c.Deliver(t.Context()); err != nil {
			t.Fatal(err)
		}
		if s, _ := state(t, pool); s != "pending" {
			return
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Fatal("the callback stayed pending")
}

func TestCallbackIsSignedAndRetriedUntilDelivered(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	vault := newSecrets(t, pool)
	srv := &target{statuses: []int{http.StatusServiceUnavailable, http.StatusTooManyRequests}}
	server := httptest.NewServer(srv)
	defer server.Close()
	ws, task := callback(t, pool, server.URL+"/hooks")
	c := NewCallbacks(pool, vault, CallbackConfig{AllowPrivateTargets: true}, slog.New(slog.DiscardHandler))

	deliverUntil(t, c, pool)

	if s, n := state(t, pool); s != "delivered" || n != 3 {
		t.Fatalf("callback %s after %d deliveries", s, n)
	}
	got := srv.requests()
	key, err := vault.SigningKey(t.Context(), ws)
	if err != nil {
		t.Fatal(err)
	}
	last := got[len(got)-1]
	if want := Sign(key, last.body, last.header.Get("X-Task-Timestamp")); last.header.Get("X-Task-Signature") != want {
		t.Fatal("the signature does not verify with the workspace signing key")
	}
	if last.header.Get("X-Task-ID") != task.String() || last.header.Get("X-Task-Status") != "succeeded" ||
		last.header.Get("X-Task-Attempt") != "1" || last.header.Get("Idempotency-Key") != got[0].header.Get("Idempotency-Key") {
		t.Fatalf("headers %v", last.header)
	}
	var body map[string]any
	if err := json.Unmarshal(last.body, &body); err != nil {
		t.Fatal(err)
	}
	data, _ := body["data"].(map[string]any)
	if body["status"] != "succeeded" || body["retry_scheduled"] != false || data["encoding"] != "json" {
		t.Fatalf("body %s", last.body)
	}
}

func TestCallbackGivesUpOnRejectionAndAfterThreeAttempts(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	srv := &target{statuses: []int{http.StatusBadRequest}}
	server := httptest.NewServer(srv)
	defer server.Close()
	callback(t, pool, server.URL)
	c := NewCallbacks(pool, newSecrets(t, pool), CallbackConfig{AllowPrivateTargets: true}, slog.New(slog.DiscardHandler))
	deliverUntil(t, c, pool)
	if s, n := state(t, pool); s != "failed" || n != 1 {
		t.Fatalf("a rejected callback is %s after %d deliveries", s, n)
	}

	busy := dbtest.New(t)
	always := &target{statuses: []int{500, 500, 500, 500}}
	server2 := httptest.NewServer(always)
	defer server2.Close()
	callback(t, busy, server2.URL)
	deliverUntil(t, NewCallbacks(busy, newSecrets(t, busy), CallbackConfig{AllowPrivateTargets: true}, slog.New(slog.DiscardHandler)), busy)
	if s, n := state(t, busy); s != "failed" || n != 3 || len(always.requests()) != 3 {
		t.Fatalf("a failing target got %d requests; callback %s after %d", len(always.requests()), s, n)
	}
}

func TestCallbacksNeverReachPrivateAddresses(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	srv := &target{}
	server := httptest.NewServer(srv)
	defer server.Close()
	callback(t, pool, server.URL)
	c := NewCallbacks(pool, newSecrets(t, pool), CallbackConfig{}, slog.New(slog.DiscardHandler))
	deliverUntil(t, c, pool)
	var lastError string
	if err := pool.QueryRow(t.Context(), "select last_error from task_callbacks").Scan(&lastError); err != nil {
		t.Fatal(err)
	}
	if s, n := state(t, pool); s != "failed" || n != 1 || len(srv.requests()) != 0 {
		t.Fatalf("a loopback target: %s after %d, %d requests (%s)", s, n, len(srv.requests()), lastError)
	}
}

func TestALargeResultIsLeftOutOfTheCallback(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	srv := &target{}
	server := httptest.NewServer(srv)
	defer server.Close()
	_, task := callback(t, pool, server.URL)
	big := `"` + strings.Repeat("x", maxCallbackResultBytes) + `"`
	if _, err := pool.Exec(t.Context(), "update task_results set data = $1 where task_id = $2", []byte(big), task); err != nil {
		t.Fatal(err)
	}
	deliverUntil(t, NewCallbacks(pool, newSecrets(t, pool), CallbackConfig{AllowPrivateTargets: true}, slog.New(slog.DiscardHandler)), pool)
	got := srv.requests()
	var body map[string]any
	if err := json.Unmarshal(got[0].body, &body); err != nil {
		t.Fatal(err)
	}
	if body["data"] != nil || body["data_omitted"] != true || len(got[0].body) > 4096 {
		t.Fatalf("a callback for a large result: %d bytes, data_omitted %v", len(got[0].body), body["data_omitted"])
	}
}

func TestNonPublicAddresses(t *testing.T) {
	t.Parallel()
	for addr, public := range map[string]bool{
		"8.8.8.8": true, "2606:4700::1111": true,
		"10.0.0.1": false, "127.0.0.1": false, "169.254.169.254": false, "100.64.0.1": false,
		"::1": false, "fd00::1": false, "64:ff9b::a9fe:a9fe": false, "64:ff9b:1::1": false,
	} {
		if got := isPublic(netip.MustParseAddr(addr).Unmap()); got != public {
			t.Errorf("isPublic(%s) = %v", addr, got)
		}
	}
}

// A workspace with a flood of due callbacks does not hold back another
// workspace's: claims take each workspace's oldest in turn, so the other's
// callback goes out in the first wave although every flooded one is older.
func TestAFloodFromOneWorkspaceDoesNotDelayAnother(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	vault := newSecrets(t, pool)
	var mu sync.Mutex
	var order []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(20 * time.Millisecond)
		mu.Lock()
		order = append(order, r.Header.Get("X-Task-ID"))
		mu.Unlock()
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	_, flooded := callback(t, pool, server.URL)
	const flood = 300
	if _, err := pool.Exec(t.Context(), `
insert into task_callbacks (task_id, workspace_id, url, event, attempt, max_attempts, next_attempt_at)
select c.task_id, c.workspace_id, c.url, 'retry', n, 400, now() - interval '1 hour'
from task_callbacks c, generate_series(2, $1::int + 1) n;
`, flood); err != nil {
		t.Fatal(err)
	}
	// Workspace names are unique; the helper names each one ws.
	if _, err := pool.Exec(t.Context(), `update workspaces set name = 'flooded'`); err != nil {
		t.Fatal(err)
	}
	_, other := callback(t, pool, server.URL)
	c := NewCallbacks(pool, vault, CallbackConfig{AllowPrivateTargets: true}, slog.New(slog.DiscardHandler))

	sent, err := c.Deliver(t.Context())
	if err != nil || sent != flood+2 {
		t.Fatalf("delivered %d (%v); want %d", sent, err, flood+2)
	}
	position := slices.Index(order, other.String())
	if position < 0 || position >= deliveryConcurrency {
		t.Fatalf("the other workspace's callback went out %dth of %d, behind the flood of %s", position+1, len(order), flooded)
	}
}
