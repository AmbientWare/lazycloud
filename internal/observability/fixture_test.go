package observability_test

import (
	"bytes"
	"context"
	"encoding/json"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

// fixture is a workspace with one active function, reports/summarize,
// built from the same rows control's deploy writes.
type fixture struct {
	t         *testing.T
	pool      *pgxpool.Pool
	exec      *execution.Execution
	obs       *observability.Observability
	workspace identity.WorkspaceID
	app       uuid.UUID
	workload  uuid.UUID
	release   uuid.UUID
}

func newFixture(t *testing.T, spec string) *fixture {
	t.Helper()
	pool := dbtest.New(t)
	f := &fixture{t: t, pool: pool, exec: execution.NewExecution(pool),
		obs: observability.NewObservability(pool, observability.Config{}, slog.New(slog.DiscardHandler))}
	f.workspace, f.app, f.workload, f.release = f.addFunction("acme", "reports", "summarize", spec)
	return f
}

// addFunction inserts a workspace (or reuses one of that name), an app, a
// workload and its active release.
func (f *fixture) addFunction(workspace, app, name, spec string) (identity.WorkspaceID, uuid.UUID, uuid.UUID, uuid.UUID) {
	f.t.Helper()
	var ws, appID, wl, rel uuid.UUID
	err := f.pool.QueryRow(f.t.Context(), `
with ws as (insert into workspaces (name) values ($1)
            on conflict (name) do update set name = excluded.name returning id),
     app as (insert into apps (workspace_id, name, state) select id, $2, 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', $3, 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, $4::jsonb, sha256('spec'), sha256('src') from wl returning id)
select ws.id, app.id, wl.id, rel.id from ws, app, wl, rel`, workspace, app, name, spec).Scan(&ws, &appID, &wl, &rel)
	if err != nil {
		f.t.Fatalf("insert function: %v", err)
	}
	if _, err := f.pool.Exec(f.t.Context(), "update workloads set active_release_id = $1, next_version = 2 where id = $2", rel, wl); err != nil {
		f.t.Fatal(err)
	}
	return identity.WorkspaceID(ws), appID, wl, rel
}

// placedContainer inserts an online host and a container of the release on
// it, ready.
func (f *fixture) placedContainer(release uuid.UUID) (compute.HostID, execution.ContainerID) {
	f.t.Helper()
	var host, container uuid.UUID
	err := f.pool.QueryRow(f.t.Context(), `
with host as (insert into hosts (name, token_hash, state, cpu_millis, memory_bytes)
              values ('h1', sha256(gen_random_uuid()::text::bytea), 'online', 4000, 1 << 32) returning id),
     ctr as (insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
             select r.workspace_id, $1, 'ready', host.id, 4, 1000, 1 << 28, now(), now()
             from host, (select a.workspace_id from releases rr join workloads w on w.id = rr.workload_id
                         join apps a on a.id = w.app_id where rr.id = $1) r returning id)
select host.id, ctr.id from host, ctr`, release).Scan(&host, &container)
	if err != nil {
		f.t.Fatalf("insert container: %v", err)
	}
	return compute.HostID(host), execution.ContainerID(container)
}

func (f *fixture) submit(n int) []execution.Task {
	f.t.Helper()
	inputs := make([]execution.TaskInput, n)
	for i := range inputs {
		inputs[i] = execution.TaskInput{Payload: execution.Payload{Encoding: execution.EncodingJSON, Data: []byte(`{"args": [1], "kwargs": {}}`)}}
	}
	tasks, err := f.exec.Submit(f.t.Context(), execution.SubmitRequest{
		Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: inputs,
	})
	if err != nil {
		f.t.Fatalf("submit: %v", err)
	}
	return tasks
}

func (f *fixture) exec1(sql string, args ...any) {
	f.t.Helper()
	if _, err := f.pool.Exec(f.t.Context(), sql, args...); err != nil {
		f.t.Fatalf("%s: %v", sql, err)
	}
}

// runHub runs a change hub for the test and waits until it listens: the
// returned subscription has taken the reset that LISTEN starts with.
func runHub(t *testing.T, pool *pgxpool.Pool, cfg observability.ChangesConfig, ws identity.WorkspaceID) (*observability.Changes, *observability.Subscription) {
	t.Helper()
	hub := observability.NewChanges(pool, cfg, nil, slog.New(slog.DiscardHandler))
	sub, _, err := hub.Subscribe(ws, nil)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Go(func() { _ = hub.Run(ctx) })
	t.Cleanup(func() { cancel(); wg.Wait() })
	select {
	case <-sub.Reset():
		if reason := sub.TakeReset(); reason != observability.ResetMissed {
			t.Fatalf("first reset %q", reason)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("the hub did not start listening")
	}
	return hub, sub
}

// nextEvent waits for the subscription's next event and decodes its data.
func nextEvent(t *testing.T, sub *observability.Subscription) (observability.ChangeEvent, apitypes.ChangeEvent) {
	t.Helper()
	select {
	case e := <-sub.Events():
		return e, decodeFrame(t, e.Frame)
	case <-time.After(10 * time.Second):
		t.Fatal("no change event")
	}
	return observability.ChangeEvent{}, apitypes.ChangeEvent{}
}

func decodeFrame(t *testing.T, frame []byte) apitypes.ChangeEvent {
	t.Helper()
	_, data, ok := bytes.Cut(frame, []byte("data: "))
	if !ok {
		t.Fatalf("frame without data: %q", frame)
	}
	var out apitypes.ChangeEvent
	if err := json.Unmarshal(bytes.TrimSpace(data), &out); err != nil {
		t.Fatalf("decode %q: %v", data, err)
	}
	return out
}

func noEvent(t *testing.T, sub *observability.Subscription, wait time.Duration) {
	t.Helper()
	select {
	case e := <-sub.Events():
		t.Fatalf("unexpected event %s", e.Frame)
	case <-time.After(wait):
	}
}
