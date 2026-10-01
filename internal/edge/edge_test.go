package edge

import (
	"bytes"
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/identity"
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
	e := newTestEdge(t, nil)
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
