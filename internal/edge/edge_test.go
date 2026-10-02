package edge

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/callbacks"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/secrets"
)

func newTestEdge(t *testing.T, pool *pgxpool.Pool) *Edge {
	t.Helper()
	logger := slog.New(slog.DiscardHandler)
	e, err := NewEdge(pool, identity.NewIdentity(pool, identity.Config{PublicURL: "http://127.0.0.1"}),
		execution.NewExecution(pool), database.NewListener(pool, logger), Config{URL: "http://lazycloud.localhost:8082"}, logger)
	if err != nil {
		t.Fatal(err)
	}
	return e
}

func version(n int) *int { return &n }

// After a rollback the active release is older than one that still has
// containers; latest waits for the active release rather than serve the one
// it rolled back from, while a forward deploy keeps the older one serving.
func TestLatestFallsBackOnlyToAnOlderRelease(t *testing.T) {
	e := newTestEdge(t, nil)
	v1, v2 := uuid.New(), uuid.New()
	e.releases[v1] = &release{id: v1, version: version(1), capacity: 1}
	e.releases[v2] = &release{id: v2, version: version(2), capacity: 1}
	ws := &workloadState{containers: map[uuid.UUID]*slot{}}
	only := func(rel uuid.UUID, v int) {
		ws.containers = map[uuid.UUID]*slot{uuid.New(): {id: uuid.New(), release: rel, version: v}}
	}

	only(v2, 2)
	if s, _ := e.pickLocked(ws, target{release: e.releases[v1], latest: true}); s != nil {
		t.Fatalf("rolled back to v1, latest picked v%d", s.version)
	}
	only(v1, 1)
	if s, _ := e.pickLocked(ws, target{release: e.releases[v2], latest: true}); s == nil || s.version != 1 {
		t.Fatalf("deployed v2, latest picked %+v; want v1 until v2 is ready", s)
	}
}

// A slow container-set read that started first never replaces a newer one.
func TestAnOlderContainerReadNeverReplacesANewerOne(t *testing.T) {
	e := newTestEdge(t, nil)
	ws := e.stateLocked(uuid.New())
	older := []execution.EndpointContainer{{Container: execution.ContainerID(uuid.New()), Version: 1}}
	newer := []execution.EndpointContainer{{Container: execution.ContainerID(uuid.New()), Version: 2}}
	e.applyLocked(ws, 2, newer)
	e.applyLocked(ws, 1, older)
	if len(ws.containers) != 1 || ws.containers[uuid.UUID(newer[0].Container)] == nil {
		t.Fatalf("containers %v; want the newer read's", ws.containers)
	}
}

// A release the publisher forgot while idle is recreated, not orphaned,
// when a request counts against it.
func TestDemandCountedAfterTheLoadWasForgottenIsPublished(t *testing.T) {
	e := newTestEdge(t, nil)
	r := &release{id: uuid.New(), maxPending: 10}
	e.mu.Lock()
	e.demandLocked(r)
	e.mu.Unlock()
	// Idle and never published: forgotten.
	if loads, _ := e.collectLoads(time.Now()); len(loads) != 0 {
		t.Fatalf("published %v for an idle release", loads)
	}
	e.mu.Lock()
	e.demandLocked(r).waiting++
	e.mu.Unlock()
	loads, _ := e.collectLoads(time.Now())
	if len(loads) != 1 || loads[0].Waiting != 1 {
		t.Fatalf("published %v; want one waiting request", loads)
	}
}

// The body budget refuses what would pass the edge-wide bound and returns
// what a request held when it ends.
func TestBufferedBodiesStayWithinTheEdgeBudget(t *testing.T) {
	e := newTestEdge(t, nil)
	if !e.bodies.reserve(maxBufferedBodyBytes - 10) {
		t.Fatal("reserve within the budget refused")
	}
	req := httptest.NewRequestWithContext(t.Context(), http.MethodPost, "/", strings.NewReader(strings.Repeat("x", 64)))
	if _, err := e.readBody(req, true); !errors.Is(err, errBufferFull) {
		t.Fatalf("read past the budget: %v; want errBufferFull", err)
	}
	if used := e.bodies.used.Load(); used != maxBufferedBodyBytes-10 {
		t.Fatalf("a refused read kept %d bytes", used-(maxBufferedBodyBytes-10))
	}
	e.bodies.used.Store(0)
	req = httptest.NewRequestWithContext(t.Context(), http.MethodPost, "/", bytes.NewReader([]byte(`{"a": 1}`)))
	body, err := e.readBody(req, true)
	if err != nil || e.bodies.used.Load() != 8 {
		t.Fatalf("read %v, %d bytes held", err, e.bodies.used.Load())
	}
	body.release()
	if e.bodies.used.Load() != 0 {
		t.Fatalf("%d bytes held after release", e.bodies.used.Load())
	}
}

// Requests for hosts the table does not know reload it at most once per
// missReloadInterval, however many arrive.
func TestUnknownHostsReloadTheRouteTableAtMostOncePerInterval(t *testing.T) {
	pool := dbtest.New(t)
	e := newTestEdge(t, pool)
	ctx := t.Context()
	started := time.Now()
	const misses = 8
	for range misses {
		if _, err := e.resolve(ctx, "nothing-0123abcd.lazycloud.localhost:8082"); !errors.Is(err, errNoRoute) {
			t.Fatalf("resolve: %v", err)
		}
	}
	// The first miss reloads at once; each later one waits out the
	// interval since the last reload began.
	if elapsed := time.Since(started); elapsed < (misses-1)*missReloadInterval {
		t.Fatalf("%d misses took %v; reloads ran more often than every %v", misses, elapsed, missReloadInterval)
	}
}

// An unknown id host is answered from memory after the first read.
func TestUnknownIDHostsAreRemembered(t *testing.T) {
	pool := dbtest.New(t)
	e := newTestEdge(t, pool)
	id := uuid.New()
	host := id.String() + ".lazycloud.localhost:8082"
	if _, err := e.resolve(t.Context(), host); !errors.Is(err, errNoRoute) {
		t.Fatalf("resolve: %v", err)
	}
	pool.Close()
	// With the database gone, only the cache can answer.
	if _, err := e.resolve(t.Context(), host); !errors.Is(err, errNoRoute) {
		t.Fatalf("second resolve: %v; want the remembered miss", err)
	}
}

// fakeForward is an agent's Forward stream as the edge sees it.
type fakeForward struct {
	grpc.ServerStream
	ctx context.Context
}

func (f *fakeForward) Context() context.Context            { return f.ctx }
func (f *fakeForward) Send(*hostproto.ForwardDown) error   { return nil }
func (f *fakeForward) Recv() (*hostproto.ForwardUp, error) { return nil, io.EOF }
func (f *fakeForward) SetHeader(metadata.MD) error         { return nil }
func (f *fakeForward) SendHeader(metadata.MD) error        { return nil }
func (f *fakeForward) SetTrailer(metadata.MD)              {}
func (f *fakeForward) SendMsg(any) error                   { return nil }
func (f *fakeForward) RecvMsg(any) error                   { return nil }

// A dead parked stream is skipped without leaving the host counted as
// waiting, and a host parks at most maxIdleStreams.
func TestStreamPoolAccountingAndIdleBound(t *testing.T) {
	e := newTestEdge(t, nil)
	host := uuid.New()
	dead, cancel := context.WithCancel(t.Context())
	cancel()
	h := &e.hosts
	h.mu.Lock()
	pool := h.poolLocked(host)
	h.mu.Unlock()

	// A request waits, a dead stream and then a live one park.
	got := make(chan *parkedStream)
	go func() {
		p, _ := h.take(t.Context(), host)
		got <- p
	}()
	waitFor(t, func() bool { h.mu.Lock(); defer h.mu.Unlock(); return pool.waiting == 1 })
	live := &parkedStream{stream: &fakeForward{ctx: t.Context()}, done: make(chan struct{})}
	h.mu.Lock()
	pool.idle = append(pool.idle,
		live,
		&parkedStream{stream: &fakeForward{ctx: dead}, done: make(chan struct{})})
	close(pool.opened)
	pool.opened = make(chan struct{})
	h.mu.Unlock()
	if p := <-got; p != live {
		t.Fatal("take returned the dead stream")
	}
	h.mu.Lock()
	waiting := pool.waiting
	h.mu.Unlock()
	if waiting != 0 {
		t.Fatalf("waiting %d after the request got a stream", waiting)
	}

	s := &Server{edge: e, hostOf: func(context.Context) (compute.HostID, bool) { return compute.HostID(host), true }}
	h.mu.Lock()
	for range maxIdleStreams {
		pool.idle = append(pool.idle, &parkedStream{stream: &fakeForward{ctx: t.Context()}, done: make(chan struct{})})
	}
	h.mu.Unlock()
	if err := s.Forward(&fakeForward{ctx: t.Context()}); err != nil {
		t.Fatalf("an extra stream: %v", err)
	}
	h.mu.Lock()
	idle := len(pool.idle)
	h.mu.Unlock()
	if idle != maxIdleStreams {
		t.Fatalf("%d idle streams; want at most %d", idle, maxIdleStreams)
	}
}

// fakeListen is an agent's Listen call.
type fakeListen struct {
	grpc.ServerStream
	ctx context.Context
}

func (f *fakeListen) Context() context.Context          { return f.ctx }
func (f *fakeListen) Send(*hostproto.ListenEvent) error { return nil }
func (f *fakeListen) SetHeader(metadata.MD) error       { return nil }
func (f *fakeListen) SendHeader(metadata.MD) error      { return nil }
func (f *fakeListen) SetTrailer(metadata.MD)            {}

// Shutdown ends Listen calls, so a graceful gRPC stop does not wait for
// agents to hang up.
func TestShutdownEndsListen(t *testing.T) {
	e := newTestEdge(t, dbtest.New(t))
	s := &Server{edge: e, hostOf: func(context.Context) (compute.HostID, bool) { return compute.HostID(uuid.New()), true }}
	ended := make(chan error, 1)
	go func() { ended <- s.Listen(&hostproto.ListenRequest{}, &fakeListen{ctx: t.Context()}) }()
	time.Sleep(50 * time.Millisecond)
	e.Shutdown()
	select {
	case <-ended:
	case <-time.After(5 * time.Second):
		t.Fatal("Listen kept running after Shutdown")
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatal("condition never held")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// A route's custom hostname serves only through a registration an owner of
// the route's workspace made: another account registering the same name
// after the owner removed theirs does not inherit the route.
func TestCustomHostnameServesOnlyThroughTheWorkspaceOwnersRegistration(t *testing.T) {
	pool := dbtest.New(t)
	e := newTestEdge(t, pool)
	ctx := t.Context()
	var owner, stranger uuid.UUID
	err := pool.QueryRow(ctx, `
with o as (insert into users (email) values ('owner@test') returning id),
     s as (insert into users (email) values ('stranger@test') returning id),
     ws as (insert into workspaces (name) values ('ws') returning id),
     m as (insert into workspace_members (workspace_id, user_id, role) select ws.id, o.id, 'owner' from ws, o),
     a as (insert into apps (workspace_id, name, state) select id, 'shop', 'active' from ws returning id),
     w as (insert into workloads (app_id, kind, name, desired_state) select id, 'asgi', 'web', 'active' from a returning id),
     r as (insert into http_routes (workload_id, subdomain, hostname) select id, 'web-0123abcd', 'shop.example.com' from w)
select o.id, s.id from o, s`).Scan(&owner, &stranger)
	if err != nil {
		t.Fatal(err)
	}
	serves := func() bool {
		routes, err := e.loadRoutes(ctx)
		if err != nil {
			t.Fatal(err)
		}
		return routes.byHostname["shop.example.com"] != nil
	}
	if _, err := pool.Exec(ctx, `insert into custom_domains (user_id, hostname, phase) values ($1, 'shop.example.com', 'ready')`, stranger); err != nil {
		t.Fatal(err)
	}
	if serves() {
		t.Fatal("a stranger's registration made the workspace's route serve")
	}
	if _, err := pool.Exec(ctx, `update custom_domains set user_id = $1`, owner); err != nil {
		t.Fatal(err)
	}
	if !serves() {
		t.Fatal("the owner's ready registration does not serve the route")
	}
	// The owner cannot remove it while the route holds it.
	var conflict *DomainConflictError
	if err := e.RemoveDomain(ctx, identity.UserID(owner), "shop.example.com"); !errors.As(err, &conflict) {
		t.Fatalf("remove a domain a route holds: %v; want DomainConflictError", err)
	}
}

// releaseFixture inserts a workspace, an endpoint release and a container,
// and returns their ids.
func releaseFixture(t *testing.T, pool *pgxpool.Pool) (workspace, workload, release, container uuid.UUID) {
	t.Helper()
	err := pool.QueryRow(t.Context(), `
with ws as (insert into workspaces (name) values ('ws') returning id),
     a as (insert into apps (workspace_id, name, state) select id, 'shop', 'active' from ws returning id),
     w as (insert into workloads (app_id, kind, name, desired_state) select id, 'endpoint', 'web', 'active' from a returning id),
     r as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
           select id, 1, '{}'::jsonb, sha256('spec'), sha256('src') from w returning id),
     c as (insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes)
           select ws.id, r.id, 'stopped', 1, 1000, 1 << 28 from ws, r returning id)
select ws.id, w.id, r.id, c.id from ws, w, r, c`).Scan(&workspace, &workload, &release, &container)
	if err != nil {
		t.Fatal(err)
	}
	return workspace, workload, release, container
}

// One retention pass deletes past a single batch, records and container
// output both, and keeps what is still inside the retention.
func TestRetentionPassDeletesEveryExpiredBatch(t *testing.T) {
	pool := dbtest.New(t)
	e := newTestEdge(t, pool)
	ctx := t.Context()
	ws, wl, rel, c := releaseFixture(t, pool)
	old := time.Now().Add(-requestRetention - time.Hour)
	rows := pruneBatch*2 + 5
	if _, err := pool.Exec(ctx, `
insert into http_requests (id, workspace_id, workload_id, release_id, method, path, status, started_at, duration_ms, request_bytes, response_bytes)
select gen_random_uuid(), $1, $2, $3, 'GET', '/', 200, $4, 1, 0, 0 from generate_series(1, $5)`, ws, wl, rel, old, rows); err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, `
insert into container_logs (container_id, stream, data, logged_at)
select $1::uuid, 'stdout', 'old', $2::timestamptz from generate_series(1, $3::int)
union all select $1::uuid, 'stdout', 'new', now()`, c, old, rows); err != nil {
		t.Fatal(err)
	}
	e.prune(ctx)
	var requests, logs int
	if err := pool.QueryRow(ctx, `select (select count(*) from http_requests), (select count(*) from container_logs)`).Scan(&requests, &logs); err != nil {
		t.Fatal(err)
	}
	if requests != 0 || logs != 1 {
		t.Fatalf("after one pass %d records and %d log lines remain; want 0 and the 1 recent line", requests, logs)
	}
}

// Records still queued when the edge stops are written, and a batch whose
// write failed is kept for the next one.
func TestQueuedRecordsAreWrittenAtShutdown(t *testing.T) {
	pool := dbtest.New(t)
	e := newTestEdge(t, pool)
	ws, wl, rel, _ := releaseFixture(t, pool)
	ctx, cancel := context.WithCancel(t.Context())
	done := make(chan struct{})
	go func() { e.writeRequests(ctx); close(done) }()
	var app uuid.UUID
	if err := pool.QueryRow(t.Context(), `select app_id from workloads where id = $1`, wl).Scan(&app); err != nil {
		t.Fatal(err)
	}
	for range 3 {
		e.queueRecord(requestRecord{
			id: uuid.New(), workspace: ws, app: app, workload: wl, release: rel, method: "GET", path: "/", status: 200,
			started: time.Now(), responseBytes: 1000,
		})
	}
	cancel()
	<-done
	// The responses' bytes reach billing as the workspace's egress, with
	// the records.
	var n int
	var egress int64
	if err := pool.QueryRow(t.Context(), `select (select count(*) from http_requests), (select coalesce(sum(bytes), 0) from egress_quarters where workspace_id = $1 and workload_id = $2)`,
		ws, wl).Scan(&n, &egress); err != nil {
		t.Fatal(err)
	}
	if n != 3 || egress != 3000 {
		t.Fatalf("%d records and %d egress bytes written at shutdown; want 3 and 3000", n, egress)
	}
}

// A release with a callback_url has each request called back once, signed,
// with its outcome and the response's status and size, even when the batch
// is written twice.
func TestRequestsOfAReleaseWithACallbackURLAreCalledBackOnce(t *testing.T) {
	pool := dbtest.New(t)
	e := newTestEdge(t, pool)
	ctx := t.Context()
	var mu sync.Mutex
	got := map[string]http.Header{}
	bodies := map[string][]byte{}
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		got[r.Header.Get("X-Request-ID")], bodies[r.Header.Get("X-Request-ID")] = r.Header.Clone(), body
		mu.Unlock()
		w.WriteHeader(http.StatusNoContent)
	}))
	t.Cleanup(receiver.Close)
	ws, wl, rel, _ := releaseFixture(t, pool)
	if _, err := pool.Exec(ctx, `update releases set spec = jsonb_build_object('callback_url', $2::text) where id = $1`, rel, receiver.URL+"/hook"); err != nil {
		t.Fatal(err)
	}
	var app uuid.UUID
	if err := pool.QueryRow(ctx, `select app_id from workloads where id = $1`, wl).Scan(&app); err != nil {
		t.Fatal(err)
	}
	outcomes := map[int]string{200: "succeeded", 404: "succeeded", 503: "failed", 499: "cancelled"}
	var batch []requestRecord
	ids := map[string]int{}
	for status := range outcomes {
		rec := requestRecord{
			id: uuid.New(), workspace: ws, app: app, workload: wl, release: rel, method: "POST", path: "/", status: status,
			started: time.Now(), duration: 1500 * time.Millisecond, responseBytes: int64(status),
		}
		ids[rec.id.String()] = status
		batch = append(batch, rec)
	}
	for range 2 {
		if err := e.insertRequests(ctx, batch); err != nil {
			t.Fatal(err)
		}
	}

	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		t.Fatal(err)
	}
	key, err := secrets.NewFileKey(raw)
	if err != nil {
		t.Fatal(err)
	}
	s := secrets.NewSecrets(pool, key)
	deliverer := callbacks.NewCallbacks(pool, s, callbacks.CallbackConfig{AllowPrivateTargets: true}, slog.New(slog.DiscardHandler))
	if n, err := deliverer.Deliver(ctx); err != nil || n != len(batch) {
		t.Fatalf("delivered %d callbacks (%v); want %d", n, err, len(batch))
	}
	if n, err := deliverer.Deliver(ctx); err != nil || n != 0 {
		t.Fatalf("a second pass delivered %d (%v); want none", n, err)
	}
	signing, err := s.SigningKey(ctx, identity.WorkspaceID(ws))
	if err != nil {
		t.Fatal(err)
	}
	for id, status := range ids {
		header, body := got[id], bodies[id]
		if header == nil {
			t.Fatalf("request %s (status %d) was not called back", id, status)
		}
		if header.Get("X-Task-Signature") != callbacks.Sign(signing, body, header.Get("X-Task-Timestamp")) {
			t.Fatalf("callback of %s is not signed with the workspace key", id)
		}
		var sent struct {
			TaskID    string `json:"task_id"`
			RequestID string `json:"request_id"`
			Status    string `json:"status"`
			Data      struct {
				StatusCode    int   `json:"status_code"`
				BodySizeBytes int64 `json:"body_size_bytes"`
			} `json:"data"`
			Error      *struct{ Kind string } `json:"error"`
			FinishedAt *time.Time             `json:"finished_at"`
		}
		if err := json.Unmarshal(body, &sent); err != nil {
			t.Fatal(err)
		}
		if sent.TaskID != id || sent.RequestID != id || sent.Status != outcomes[status] || header.Get("X-Task-Status") != outcomes[status] {
			t.Fatalf("callback of a %d request reports %+v; want %s for %s", status, sent, outcomes[status], id)
		}
		if sent.Data.StatusCode != status || sent.Data.BodySizeBytes != int64(status) || sent.FinishedAt == nil {
			t.Fatalf("callback of a %d request carries %+v", status, sent)
		}
		if (sent.Error != nil) != (status == 503) {
			t.Fatalf("callback of a %d request has error %+v", status, sent.Error)
		}
	}
}

// Past maxPendingCallbacks waiting callbacks of one release, the edge
// records further request callbacks as failed with the reason instead of
// queueing them, so one endpoint's flood cannot fill the shared queue.
func TestAReleasesCallbacksPastTheCapAreDroppedWithTheReason(t *testing.T) {
	pool := dbtest.New(t)
	e := newTestEdge(t, pool)
	ctx := t.Context()
	ws, wl, rel, _ := releaseFixture(t, pool)
	if _, err := pool.Exec(ctx, `update releases set spec = '{"callback_url": "https://hooks.example.com/r"}' where id = $1`, rel); err != nil {
		t.Fatal(err)
	}
	var app uuid.UUID
	if err := pool.QueryRow(ctx, `select app_id from workloads where id = $1`, wl).Scan(&app); err != nil {
		t.Fatal(err)
	}
	records := func(n int) []requestRecord {
		out := make([]requestRecord, n)
		for i := range out {
			out[i] = requestRecord{id: uuid.New(), workspace: ws, app: app, workload: wl, release: rel, method: "POST", path: "/", status: 200, started: time.Now()}
		}
		return out
	}
	if err := e.insertRequests(ctx, records(maxPendingCallbacks-2)); err != nil {
		t.Fatal(err)
	}
	if err := e.insertRequests(ctx, records(5)); err != nil {
		t.Fatal(err)
	}
	var pending, dropped int
	var reason string
	if err := pool.QueryRow(ctx, `
select count(*) filter (where state = 'pending'), count(*) filter (where state = 'failed'),
       coalesce(max(last_error) filter (where state = 'failed'), '')
from task_callbacks where release_id = $1`, rel).Scan(&pending, &dropped, &reason); err != nil {
		t.Fatal(err)
	}
	if pending != maxPendingCallbacks || dropped != 3 || !strings.Contains(reason, "were already waiting") {
		t.Fatalf("%d pending and %d dropped (%q); want %d and 3 with the reason", pending, dropped, reason, maxPendingCallbacks)
	}
}

// Registering a custom domain needs a plan that includes them; the provider
// is never asked otherwise.
func TestDomainRegistrationFollowsThePlan(t *testing.T) {
	pool := dbtest.New(t)
	logger := slog.New(slog.DiscardHandler)
	provider := &countingProvider{}
	e, err := NewEdge(pool, identity.NewIdentity(pool, identity.Config{PublicURL: "http://127.0.0.1"}),
		execution.NewExecution(pool), database.NewListener(pool, logger), Config{URL: "http://lazycloud.localhost:8082", Domains: provider}, logger)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(t.Context(), `insert into workspaces (name) values ('ws')`); err != nil {
		t.Fatal(err)
	}
	dbtest.OwnWorkspaces(t, pool)
	var user uuid.UUID
	if err := pool.QueryRow(t.Context(), `update billing_accounts set complimentary_since = null returning user_id`).Scan(&user); err != nil {
		t.Fatal(err)
	}
	var payment *billing.PaymentRequiredError
	if _, err := e.RegisterDomain(t.Context(), identity.UserID(user), "shop.example.com"); !errors.As(err, &payment) || provider.created != 0 {
		t.Fatalf("register on the Free plan: %v, %d provider calls; want PaymentRequiredError and none", err, provider.created)
	}
}

// countingProvider counts hostnames it was asked to create and refuses them.
type countingProvider struct {
	Provider
	created int
}

func (p *countingProvider) CreateHostname(context.Context, string) (ProviderHostname, error) {
	p.created++
	return ProviderHostname{}, errors.New("not reached in this test")
}

// A paused app's release hosts take working-tree calls, as the API's
// submit does, and refuse its deployed versions.
func TestReleaseHostsOfAPausedAppTakeOnlyWorkingTreeCalls(t *testing.T) {
	pool := dbtest.New(t)
	e := newTestEdge(t, pool)
	ctx := t.Context()
	var deployed, workingTree uuid.UUID
	err := pool.QueryRow(ctx, `
with ws as (insert into workspaces (name) values ('ws') returning id),
     a as (insert into apps (workspace_id, name, state) select id, 'reports', 'paused' from ws returning id),
     w as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'summarize', 'active' from a returning id),
     d as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
           select id, 1, '{"kind": "function", "name": "summarize"}', sha256('v1'), sha256('src') from w returning id),
     r as (insert into releases (workload_id, spec, spec_digest, source_sha256)
           select id, '{"kind": "function", "name": "summarize"}', sha256('run'), sha256('src') from w returning id)
select d.id, r.id from d, r`).Scan(&deployed, &workingTree)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(ctx, "update workloads set active_release_id = $1", deployed); err != nil {
		t.Fatal(err)
	}
	dbtest.OwnWorkspaces(t, pool)
	run, err := e.readID(ctx, workingTree)
	if err != nil || !run.workload.accepting {
		t.Fatalf("working-tree host of a paused app %+v %v, want accepting", run.workload, err)
	}
	version, err := e.readID(ctx, deployed)
	if err != nil || version.workload.accepting {
		t.Fatalf("deployed version host of a paused app %+v %v, want refusing", version.workload, err)
	}
}
