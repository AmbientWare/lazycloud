package execution

import (
	"context"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Listings read one page, so their cost does not grow with retained
// history: 10,000 more finished tasks on 1,000 more stopped containers of
// one busy function, nine in ten of them spawned by one task, leave each
// listing's reads where they were. That holds for the busy function and for
// what its history and live containers bury: a quiet workload beside it, a
// quiet app, rare statuses, root tasks below a deep cursor and a quiet
// workspace. A burst of churn that vacuum has not reached tops the history,
// and each statement is explained with custom and generic plans, as pgx
// prepares statements in production.
func TestListingsReadOnePageOfAnIndex(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	f := newRelease(t, pool, `{"autoscaler": {"max_containers": 5}}`)
	host := newHost(t, pool)
	queueTasks(t, pool, f, 20, 0)
	readyContainer(t, pool, f, host, time.Minute)
	exec(t, pool, `update containers set workload_id = $1`, f.workload)
	exec(t, pool, `insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts)
select $1, $2, $3, 'failed', 1, 1 from generate_series(1, 3)`, f.workspace, f.workload, f.release)
	var queued uuid.UUID
	if err := pool.QueryRow(t.Context(), "select id from tasks where status = 'queued' limit 1").Scan(&queued); err != nil {
		t.Fatal(err)
	}
	// A quiet function beside the busy one, and a quiet app, each with three
	// tasks and three stopped containers.
	exec(t, pool, `
with quiet as (insert into apps (workspace_id, name, state) values ($1, 'quiet', 'active') returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state)
            select $2::uuid, 'function', 'q', 'active' union all select id, 'function', 'q', 'active' from quiet
            returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, '{}', sha256('q'), sha256('q') from wl returning id, workload_id),
     ctr as (insert into containers (workspace_id, release_id, workload_id, state, slots, cpu_millis, memory_bytes)
             select $1, rel.id, rel.workload_id, 'stopped', 1, 1000, 1 << 28 from rel, generate_series(1, 3))
insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts)
select $1, rel.workload_id, rel.id, 'succeeded', 1, 1 from rel, generate_series(1, 3)`, f.workspace, f.app)
	var quietWorkload uuid.UUID
	if err := pool.QueryRow(t.Context(), "select id from workloads where app_id = $1 and name = 'q'", f.app).Scan(&quietWorkload); err != nil {
		t.Fatal(err)
	}
	// Other workspaces' work, as the tables hold in production.
	var quietWorkspace identity.WorkspaceID
	for range 30 {
		g := newRelease(t, pool, `{}`)
		quietWorkspace = identity.WorkspaceID(g.workspace)
		exec(t, pool, `insert into containers (workspace_id, release_id, workload_id, state, slots, cpu_millis, memory_bytes)
select $1, $2, $3, 'stopped', 1, 1000, 1 << 28 from generate_series(1, 500)`, g.workspace, g.release, g.workload)
		exec(t, pool, `insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts)
select $1, $2, $3, 'succeeded', 1, 1 from generate_series(1, 300)`, g.workspace, g.workload, g.release)
	}
	// The history runs on version 2, and one of its tasks spawns the rest.
	busy := newVersion(t, pool, f, `{}`)
	var spawner uuid.UUID
	err := pool.QueryRow(t.Context(), `insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts)
values ($1, $2, $3, 'succeeded', 1, 1) returning id`, f.workspace, f.workload, busy.release).Scan(&spawner)
	if err != nil {
		t.Fatal(err)
	}
	// The busy function serves from many live containers.
	exec(t, pool, `insert into containers (workspace_id, release_id, workload_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
select $1, $2, $3, 'ready', $4, 1, 1000, 1 << 28, now(), now() from generate_series(1, 600)`, f.workspace, busy.release, f.workload, host)
	// Vacuum runs where the history says so, and not over the recent churn.
	exec(t, pool, "alter table tasks set (autovacuum_enabled = off)")
	exec(t, pool, "alter table containers set (autovacuum_enabled = off)")
	// grow adds tasks of history, then churn that vacuum has not reached:
	// 2,000 tasks run through queued, running and succeeded, and 200
	// containers start and stop. A transaction open since before the churn,
	// as any concurrent statement may be, keeps every version the churn
	// replaced from being marked dead in the indexes until the returned
	// release ends it, so listings walk them as they may in production.
	grow := func(tasks int) (release func()) {
		addHistory(t, pool, busy, host, tasks)
		exec(t, pool, `update tasks set parent_task_id = $1, root_task_id = $1
where workload_id = $2 and parent_task_id is null and id > $1 and right(id::text, 1) <> '0'`, spawner, f.workload)
		settle(t, pool)
		held, err := pool.BeginTx(t.Context(), pgx.TxOptions{IsoLevel: pgx.RepeatableRead})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := held.Exec(t.Context(), "select 1"); err != nil {
			t.Fatal(err)
		}
		exec(t, pool, `insert into tasks (workspace_id, workload_id, release_id, status, max_attempts)
select $1, $2, $3, 'queued', 1 from generate_series(1, 2000)`, f.workspace, f.workload, busy.release)
		exec(t, pool, `insert into containers (workspace_id, release_id, workload_id, state, slots, cpu_millis, memory_bytes)
select $1, $2, $3, 'pending', 1, 1000, 1 << 28 from generate_series(1, 200)`, f.workspace, busy.release, f.workload)
		exec(t, pool, `update tasks set status = 'running', attempt_count = 1, started_at = now() where status = 'queued' and release_id = $1`, busy.release)
		exec(t, pool, `update containers set state = 'starting', host_id = $2, assigned_at = now() where state = 'pending' and release_id = $1`, busy.release, host)
		exec(t, pool, `update tasks set status = 'succeeded', finished_at = now() where status = 'running' and release_id = $1`, busy.release)
		exec(t, pool, `update containers set state = 'stopped', stop_reason = 'stopped', stopped_at = now() where state = 'starting' and release_id = $1`, busy.release)
		exec(t, pool, "analyze")
		return func() {
			if err := held.Rollback(t.Context()); err != nil {
				t.Fatal(err)
			}
		}
	}

	ws := identity.WorkspaceID(f.workspace)
	app, quietApp, function, none := "app", "quiet", "f", "zz"
	failed, cancelled := TaskFailed, TaskCancelled
	prefix := queued.String()[:8]
	tasks := func(workspace identity.WorkspaceID, filter TaskFilter) func(context.Context, *Execution) error {
		return func(ctx context.Context, e *Execution) error {
			_, err := e.ListTasks(ctx, workspace, filter, 100, "")
			return err
		}
	}
	containers := func(workspace identity.WorkspaceID, filter ContainerFilter) func(context.Context, *Execution) error {
		return func(ctx context.Context, e *Execution) error {
			_, err := e.ListContainers(ctx, workspace, filter, 100, "")
			return err
		}
	}
	listings := []listing{
		{"tasks", tasks(ws, TaskFilter{})},
		{"tasks of a quiet workspace", tasks(quietWorkspace, TaskFilter{})},
		{"failed tasks", tasks(ws, TaskFilter{Status: &failed})},
		{"cancelled tasks", tasks(ws, TaskFilter{Status: &cancelled})},
		{"root tasks", tasks(ws, TaskFilter{RootOnly: true})},
		{"failed root tasks", tasks(ws, TaskFilter{Status: &failed, RootOnly: true})},
		{"root tasks below a deep cursor", func(ctx context.Context, e *Execution) error {
			// Half way down the busy function's tasks.
			var cursor uuid.UUID
			err := pool.QueryRow(ctx, `select id from tasks where workload_id = $1
order by id desc offset (select count(*) / 2 from tasks where workload_id = $1) limit 1`, f.workload).Scan(&cursor)
			if err != nil {
				return err
			}
			_, err = e.ListTasks(ctx, ws, TaskFilter{RootOnly: true}, 100, cursor.String())
			return err
		}},
		{"app tasks", tasks(ws, TaskFilter{App: &app})},
		{"app root tasks", tasks(ws, TaskFilter{App: &app, RootOnly: true})},
		{"failed function tasks", tasks(ws, TaskFilter{App: &app, Function: &function, Status: &failed})},
		{"tasks searched by function", tasks(ws, TaskFilter{Search: &function})},
		{"tasks searched by id", tasks(ws, TaskFilter{Search: &prefix})},
		{"tasks searched for nothing", tasks(ws, TaskFilter{Search: &none})},
		{"app tasks searched for nothing", tasks(ws, TaskFilter{App: &app, Search: &none})},
		{"containers", containers(ws, ContainerFilter{})},
		{"containers of a quiet workspace", containers(quietWorkspace, ContainerFilter{})},
		{"live containers", containers(ws, ContainerFilter{Live: true})},
		{"app containers", containers(ws, ContainerFilter{App: &app})},
		{"quiet app containers", containers(ws, ContainerFilter{App: &quietApp})},
		{"quiet workload containers", containers(ws, ContainerFilter{Workload: &quietWorkload})},
		{"live quiet app containers", containers(ws, ContainerFilter{Live: true, App: &quietApp})},
		{"sandboxes", func(ctx context.Context, e *Execution) error {
			_, err := e.ListSandboxes(ctx, ws, SandboxFilter{}, 100, "")
			return err
		}},
		{"sandboxes searched", func(ctx context.Context, e *Execution) error {
			search := "box"
			_, err := e.ListSandboxes(ctx, ws, SandboxFilter{Search: &search}, 100, "")
			return err
		}},
	}
	release := grow(3_000)
	small := listingCost(t, pool, listings, "3,000 finished tasks", 0)
	release()
	release = grow(10_000)
	large := listingCost(t, pool, listings, "13,000 finished tasks", 500)
	release()
	constantCost(t, small, large, "10,000 finished tasks and 1,000 stopped containers")
}

// listing is one listing call whose statements a test explains.
type listing struct {
	name string
	list func(context.Context, *Execution) error
}

// recorder keeps the statements a pool's connections run.
type recorder struct {
	mu   sync.Mutex
	runs []scan
}

func (s *recorder) TraceQueryStart(ctx context.Context, _ *pgx.Conn, data pgx.TraceQueryStartData) context.Context {
	s.mu.Lock()
	defer s.mu.Unlock()
	name, _, _ := strings.Cut(strings.TrimPrefix(data.SQL, "-- name: "), " ")
	s.runs = append(s.runs, scan{name: name, query: data.SQL, args: data.Args})
	return ctx
}

func (*recorder) TraceQueryEnd(context.Context, *pgx.Conn, pgx.TraceQueryEndData) {}

// listingCost runs each listing through Execution, then explains every
// statement it ran as costAt does, keyed by listing and statement.
func listingCost(t *testing.T, pool *pgxpool.Pool, listings []listing, label string, limit int) map[string]int {
	t.Helper()
	config := pool.Config().Copy()
	recorded := &recorder{}
	config.ConnConfig.Tracer = recorded
	traced, err := pgxpool.NewWithConfig(t.Context(), config)
	if err != nil {
		t.Fatal(err)
	}
	defer traced.Close()
	e := NewExecution(traced)
	var scans []scan
	for _, l := range listings {
		recorded.runs = nil
		if err := l.list(t.Context(), e); err != nil {
			t.Fatalf("%s: %v", l.name, err)
		}
		for _, run := range recorded.runs {
			scans = append(scans, scan{name: l.name + ", " + run.name, query: run.query, args: run.args})
		}
	}
	return costAt(t, pool, scans, label, limit)
}

// settle vacuums until every page of tasks and containers is all-visible, as
// the history autovacuum has passed over is in production. One vacuum right
// after the writes does not always mark every page.
func settle(t *testing.T, pool *pgxpool.Pool) {
	t.Helper()
	for {
		exec(t, pool, "vacuum analyze")
		var settled bool
		err := pool.QueryRow(t.Context(), `select bool_and(relallvisible = relpages) from pg_class
where relname in ('tasks', 'containers')`).Scan(&settled)
		if err != nil {
			t.Fatal(err)
		}
		if settled {
			return
		}
		select {
		case <-t.Context().Done():
			t.Fatal("tasks and containers never became all-visible")
		case <-time.After(100 * time.Millisecond):
		}
	}
}
