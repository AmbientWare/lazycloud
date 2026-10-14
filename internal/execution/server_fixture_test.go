package execution

import (
	"context"
	"log/slog"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

type functionFixture struct {
	workspace identity.WorkspaceID
	release   uuid.UUID
}

// deployedFunction inserts an active function "reports/summarize" whose
// active release has spec.
func deployedFunction(t *testing.T, pool *pgxpool.Pool, spec string) functionFixture {
	t.Helper()
	var f functionFixture
	var ws uuid.UUID
	err := pool.QueryRow(t.Context(), `
with ws as (insert into workspaces (name) values ('ws') returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'reports', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'summarize', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, $1::jsonb, sha256('spec'), sha256('src') from wl returning id, workload_id)
select ws.id, rel.id from ws, rel`, spec).Scan(&ws, &f.release)
	if err != nil {
		t.Fatalf("insert function: %v", err)
	}
	dbtest.OwnWorkspaces(t, pool)
	if _, err := pool.Exec(t.Context(), "update workloads set active_release_id = $1, next_version = 2", f.release); err != nil {
		t.Fatal(err)
	}
	f.workspace = identity.WorkspaceID(ws)
	return f
}

// placedContainer inserts an online host and a container of release in
// state on it.
func placedContainer(t *testing.T, pool *pgxpool.Pool, f functionFixture, state ContainerState, slots int) (compute.HostID, ContainerID) {
	t.Helper()
	var host, container uuid.UUID
	err := pool.QueryRow(t.Context(), `
with host as (insert into hosts (name, token_hash, state, cpu_millis, memory_bytes)
              values ('h', sha256(gen_random_uuid()::text::bytea), 'online', 4000, 1 << 32) returning id),
     ctr as (insert into containers (workspace_id, release_id, workload_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
             select $1, r.id, r.workload_id, $3, host.id, $4, 1000, 1 << 28, now() from host, releases r where r.id = $2 returning id)
select host.id, ctr.id from host, ctr`, uuid.UUID(f.workspace), f.release, string(state), slots).Scan(&host, &container)
	if err != nil {
		t.Fatalf("insert container: %v", err)
	}
	return compute.HostID(host), ContainerID(container)
}

// listen runs a listener on the execution channels for the test.
func listen(t *testing.T, pool *pgxpool.Pool) *database.Listener {
	t.Helper()
	l := database.NewListener(pool, slog.New(slog.DiscardHandler),
		database.ChannelHost, database.ChannelTask, database.ChannelTaskFinished, database.ChannelClaim)
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Go(func() { _ = l.Run(ctx) })
	t.Cleanup(func() {
		cancel()
		wg.Wait()
	})
	return l
}

func jsonInputs(n int) []TaskInput {
	inputs := make([]TaskInput, n)
	for i := range inputs {
		inputs[i] = TaskInput{Payload: Payload{Encoding: EncodingJSON, Data: []byte(`{"args": [1], "kwargs": {}}`)}}
	}
	return inputs
}

func submit(t *testing.T, e *Execution, f functionFixture, n int) []Task {
	t.Helper()
	tasks, err := e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: jsonInputs(n)})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	return tasks
}

func status(t *testing.T, pool *pgxpool.Pool, task TaskID) TaskStatus {
	t.Helper()
	var s string
	if err := pool.QueryRow(t.Context(), "select status from tasks where id = $1", uuid.UUID(task)).Scan(&s); err != nil {
		t.Fatal(err)
	}
	return TaskStatus(s)
}

// statements counts the statements a pool finished.
type statements struct {
	n    atomic.Int64
	done chan struct{}
}

func (*statements) TraceQueryStart(ctx context.Context, _ *pgx.Conn, _ pgx.TraceQueryStartData) context.Context {
	return ctx
}

func (s *statements) TraceQueryEnd(context.Context, *pgx.Conn, pgx.TraceQueryEndData) {
	s.n.Add(1)
	select {
	case s.done <- struct{}{}:
	default:
	}
}

// take returns how many statements finished since the last take.
func (s *statements) take() int64 { return s.n.Swap(0) }

// await waits until n statements finished since the last take.
func (s *statements) await(t *testing.T, n int64) {
	t.Helper()
	for s.n.Load() < n {
		select {
		case <-s.done:
		case <-time.After(10 * time.Second):
			t.Fatalf("%d of %d statements finished", s.n.Load(), n)
		}
	}
}

// countedExecution is an Execution on a pool of its own over pool's
// database, whose finished statements it counts.
func countedExecution(t *testing.T, pool *pgxpool.Pool) (*Execution, *statements) {
	t.Helper()
	counter := &statements{done: make(chan struct{}, 1)}
	cfg := pool.Config().Copy()
	cfg.ConnConfig.Tracer = counter
	counted, err := pgxpool.NewWithConfig(t.Context(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(counted.Close)
	return NewExecution(counted), counter
}

// waiting starts call and returns once it has run the statements of probe,
// one pass of the same call without its wait. Nothing call reads after that
// is committed yet, so a change committed afterwards reaches call only
// through a wake.
func waiting[T any](t *testing.T, pool *pgxpool.Pool, probe func(*Execution), call func(*Execution) T) <-chan T {
	t.Helper()
	e, counter := countedExecution(t, pool)
	probe(e)
	pass := counter.take()
	out := make(chan T, 1)
	go func() { out <- call(e) }()
	counter.await(t, pass)
	return out
}

// notifications collects the payloads committed on channel; each call
// returns those committed since the last.
func notifications(t *testing.T, pool *pgxpool.Pool, channel database.Channel) func() []string {
	t.Helper()
	conn, err := pool.Acquire(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(conn.Release)
	if _, err := conn.Exec(t.Context(), "listen "+pgx.Identifier{string(channel)}.Sanitize()); err != nil {
		t.Fatal(err)
	}
	return func() []string {
		t.Helper()
		// Notifications arrive in commit order, so a later commit's marks
		// the end of what came before.
		end := uuid.NewString()
		if _, err := pool.Exec(t.Context(), "select pg_notify($1, $2)", string(channel), end); err != nil {
			t.Fatal(err)
		}
		var got []string
		for {
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			n, err := conn.Conn().WaitForNotification(ctx)
			cancel()
			if err != nil {
				t.Fatalf("wait for notifications: %v", err)
			}
			if n.Payload == end {
				return got
			}
			got = append(got, n.Payload)
		}
	}
}
