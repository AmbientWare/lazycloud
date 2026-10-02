package execution

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func discardLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

type releaseFixture struct {
	workspace, app, workload, release uuid.UUID
}

// newRelease inserts an active app, an active workload and its active release
// with spec. Resources default to 1000 millicores and 512 MiB.
func newRelease(t *testing.T, pool *pgxpool.Pool, spec string) releaseFixture {
	t.Helper()
	var f releaseFixture
	err := pool.QueryRow(t.Context(), `
with ws as (insert into workspaces (name) values ('ws-' || substr(md5(random()::text), 1, 8)) returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, '{"resources": {"cpu_millis": 1000, "memory_mib": 512}}'::jsonb || $1::jsonb,
                    sha256('spec'), sha256('src') from wl returning id)
select ws.id, app.id, wl.id, rel.id from ws, app, wl, rel`, spec).Scan(&f.workspace, &f.app, &f.workload, &f.release)
	if err != nil {
		t.Fatalf("insert release: %v", err)
	}
	dbtest.OwnWorkspaces(t, pool)
	exec(t, pool, "update workloads set active_release_id = $1 where id = $2", f.release, f.workload)
	return f
}

// newVersion adds a second release to f's workload without activating it.
func newVersion(t *testing.T, pool *pgxpool.Pool, f releaseFixture, spec string) releaseFixture {
	t.Helper()
	next := f
	err := pool.QueryRow(t.Context(), `
insert into releases (workload_id, version, spec, spec_digest, source_sha256)
select $1, 2, '{"resources": {"cpu_millis": 1000, "memory_mib": 512}}'::jsonb || $2::jsonb, sha256('spec2'), sha256('src')
returning id`, f.workload, spec).Scan(&next.release)
	if err != nil {
		t.Fatalf("insert release: %v", err)
	}
	return next
}

func exec(t *testing.T, pool *pgxpool.Pool, sql string, args ...any) {
	t.Helper()
	if _, err := pool.Exec(t.Context(), sql, args...); err != nil {
		t.Fatalf("%s: %v", sql, err)
	}
}

// queueTasks inserts n queued tasks that become available after delay.
func queueTasks(t *testing.T, pool *pgxpool.Pool, f releaseFixture, n int, delay time.Duration) {
	t.Helper()
	exec(t, pool, `
insert into tasks (workspace_id, workload_id, release_id, status, max_attempts, available_at)
select $1, $2, $3, 'queued', 1, now() + make_interval(secs => $5)
from generate_series(1, $4)`, f.workspace, f.workload, f.release, n, delay.Seconds())
}

func newHost(t *testing.T, pool *pgxpool.Pool) uuid.UUID {
	t.Helper()
	var id uuid.UUID
	err := pool.QueryRow(t.Context(), `
insert into hosts (name, token_hash, state, cpu_millis, memory_bytes, last_seen_at)
values ('h', sha256(random()::text::bytea), 'online', 64000, 1::bigint << 36, now()) returning id`).Scan(&id)
	if err != nil {
		t.Fatalf("insert host: %v", err)
	}
	return id
}

// readyContainer inserts a ready container of f on host that became ready
// readyFor ago.
func readyContainer(t *testing.T, pool *pgxpool.Pool, f releaseFixture, host uuid.UUID, readyFor time.Duration) uuid.UUID {
	t.Helper()
	var id uuid.UUID
	err := pool.QueryRow(t.Context(), `
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
values ($1, $2, 'ready', $3, 1, 1000, 1 << 29, now() - make_interval(secs => $4), now() - make_interval(secs => $4))
returning id`, f.workspace, f.release, host, readyFor.Seconds()).Scan(&id)
	if err != nil {
		t.Fatalf("insert container: %v", err)
	}
	return id
}

// attemptOn inserts a task of f with one attempt on container. A zero
// finishedAgo leaves the attempt and task running.
func attemptOn(t *testing.T, db interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}, f releaseFixture, container uuid.UUID, finishedAgo time.Duration,
) uuid.UUID {
	t.Helper()
	status, state := "running", "running"
	var finished *time.Duration
	if finishedAgo > 0 {
		status, state, finished = "succeeded", "succeeded", &finishedAgo
	}
	var finishedSecs *float64
	if finished != nil {
		s := finished.Seconds()
		finishedSecs = &s
	}
	var attempt, task uuid.UUID
	err := db.QueryRow(t.Context(), `
with task as (
    insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts, started_at)
    values ($1, $2, $3, $5, 1, 1, now()) returning id
)
insert into attempts (task_id, number, container_id, state, deadline_at, finished_at)
select task.id, 1, $4, $6, now() + interval '1 hour', now() - make_interval(secs => $7::float8) from task
returning id, task_id`,
		f.workspace, f.workload, f.release, container, status, state, finishedSecs).Scan(&attempt, &task)
	if err != nil {
		t.Fatalf("insert attempt: %v", err)
	}
	var updated uuid.UUID
	if err := db.QueryRow(t.Context(), "update tasks set current_attempt_id = $1 where id = $2 returning id", attempt, task).Scan(&updated); err != nil {
		t.Fatalf("link attempt: %v", err)
	}
	return attempt
}

func containerStates(t *testing.T, pool *pgxpool.Pool, release uuid.UUID) map[ContainerState]int {
	t.Helper()
	rows, err := pool.Query(t.Context(), "select state, count(*) from containers where release_id = $1 group by state", release)
	if err != nil {
		t.Fatal(err)
	}
	states := map[ContainerState]int{}
	for rows.Next() {
		var state string
		var n int
		if err := rows.Scan(&state, &n); err != nil {
			t.Fatal(err)
		}
		states[ContainerState(state)] = n
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return states
}

func containerState(t *testing.T, pool *pgxpool.Pool, id uuid.UUID) ContainerState {
	t.Helper()
	var state string
	if err := pool.QueryRow(t.Context(), "select state from containers where id = $1", id).Scan(&state); err != nil {
		t.Fatal(err)
	}
	return ContainerState(state)
}

func plan(t *testing.T, e *Execution) PlanResult {
	t.Helper()
	result, err := e.Plan(t.Context(), discardLogger())
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	return result
}

func TestPlanScalesWithDemandWithinLimits(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := newRelease(t, pool, `{"concurrency": 2, "autoscaler": {"tasks_per_container": 4, "max_containers": 3}}`)

	queueTasks(t, pool, f, 5, 0)
	// A retry waiting for its delay is not demand yet.
	queueTasks(t, pool, f, 20, time.Hour)
	plan(t, e)
	if got := containerStates(t, pool, f.release)[ContainerPending]; got != 2 {
		t.Fatalf("5 available tasks at 4 per container: %d pending, want 2", got)
	}
	var slots, cpu, memory int64
	if err := pool.QueryRow(t.Context(), "select slots, cpu_millis, memory_bytes from containers where release_id = $1 limit 1", f.release).Scan(&slots, &cpu, &memory); err != nil {
		t.Fatal(err)
	}
	if slots != 2 || cpu != 1000 || memory != 512<<20 {
		t.Fatalf("container slots %d cpu %d memory %d; want the spec's concurrency and resources", slots, cpu, memory)
	}

	queueTasks(t, pool, f, 50, 0)
	plan(t, e)
	if got := containerStates(t, pool, f.release)[ContainerPending]; got != 3 {
		t.Fatalf("demand above the maximum: %d pending, want max_containers 3", got)
	}

	// Demand vanishes before placement: pending containers stop directly.
	exec(t, pool, "update tasks set status = 'cancelled', finished_at = now() where release_id = $1", f.release)
	plan(t, e)
	var stopped int
	if err := pool.QueryRow(t.Context(), "select count(*) from containers where release_id = $1 and state = 'stopped' and stop_reason = 'stopped'", f.release).Scan(&stopped); err != nil {
		t.Fatal(err)
	}
	if stopped != 3 {
		t.Fatalf("%d pending containers stopped after demand ended, want 3", stopped)
	}
}

func TestMinimumAppliesOnlyToTheActiveRelease(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	spec := `{"autoscaler": {"min_containers": 2, "max_containers": 5}}`
	active := newRelease(t, pool, spec)
	inactive := newVersion(t, pool, active, spec)
	queueTasks(t, pool, inactive, 1, 0)

	plan(t, e)
	if got := containerStates(t, pool, active.release)[ContainerPending]; got != 2 {
		t.Fatalf("active release without demand: %d pending, want min_containers 2", got)
	}
	if got := containerStates(t, pool, inactive.release)[ContainerPending]; got != 1 {
		t.Fatalf("inactive release with one task: %d pending, want 1 (no minimum)", got)
	}

	// A stopped workload keeps no minimum.
	exec(t, pool, "update workloads set desired_state = 'stopped' where id = $1", active.workload)
	plan(t, e)
	states := containerStates(t, pool, active.release)
	if states[ContainerPending] != 0 || states[ContainerStopped] != 2 {
		t.Fatalf("stopped workload: %v; want every pending container stopped", states)
	}
}

// Two planners racing on one release never create more than max_containers:
// the planning lock lets one decide while the other skips.
func TestConcurrentPlannersRespectMaxContainers(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := newRelease(t, pool, `{"autoscaler": {"max_containers": 5}}`)
	queueTasks(t, pool, f, 100, 0)

	for round := range 20 {
		var wg sync.WaitGroup
		errs := make(chan error, 4)
		for range 4 {
			wg.Go(func() {
				if _, err := e.Plan(t.Context(), discardLogger()); err != nil {
					errs <- err
				}
			})
		}
		wg.Wait()
		close(errs)
		for err := range errs {
			t.Fatal(err)
		}
		if got := containerStates(t, pool, f.release)[ContainerPending]; got != 5 {
			t.Fatalf("round %d: %d live containers, want max_containers 5", round, got)
		}
		exec(t, pool, "update containers set state = 'stopped', stopped_at = now() where release_id = $1", f.release)
	}
}

func TestIdleDrainRespectsKeepWarmAndRunningAttempts(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := newRelease(t, pool, `{"keep_warm_seconds": 60, "autoscaler": {"max_containers": 5}}`)
	host := newHost(t, pool)
	idle := readyContainer(t, pool, f, host, 5*time.Minute)
	recent := readyContainer(t, pool, f, host, 5*time.Minute)
	attemptOn(t, pool, f, recent, 10*time.Second)
	justReady := readyContainer(t, pool, f, host, 10*time.Second)
	busy := readyContainer(t, pool, f, host, 5*time.Minute)
	attemptOn(t, pool, f, busy, 0)

	listen, err := pool.Acquire(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer listen.Release()
	if _, err := listen.Exec(t.Context(), "listen lc_host"); err != nil {
		t.Fatal(err)
	}

	result := plan(t, e)
	if result.Drained != 1 || containerState(t, pool, idle) != ContainerDraining {
		t.Fatalf("drained %d; want only the container idle past keep_warm_seconds", result.Drained)
	}
	for _, id := range []uuid.UUID{recent, justReady, busy} {
		if state := containerState(t, pool, id); state != ContainerReady {
			t.Fatalf("container %s is %s, want ready", id, state)
		}
	}
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	notification, err := listen.Conn().WaitForNotification(ctx)
	if err != nil || notification.Payload != host.String() {
		t.Fatalf("host wake: %v %v; want lc_host for the drained container's host", notification, err)
	}
}

// A claim holding the container FOR SHARE with its attempt not yet committed
// must not lose the container to a drain: the planner skips it without
// blocking, and afterwards sees the running attempt.
func TestDrainSkipsAContainerBeingClaimed(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := newRelease(t, pool, `{"keep_warm_seconds": 0, "autoscaler": {"max_containers": 5}}`)
	host := newHost(t, pool)
	container := readyContainer(t, pool, f, host, time.Minute)

	claim, err := pool.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = claim.Rollback(context.WithoutCancel(t.Context())) }()
	var locked uuid.UUID
	if err := claim.QueryRow(t.Context(), "select id from containers where id = $1 and state = 'ready' for share", container).Scan(&locked); err != nil {
		t.Fatal(err)
	}
	attemptOn(t, claim, f, container, 0)

	planned := make(chan PlanResult, 1)
	go func() {
		result, err := e.Plan(t.Context(), discardLogger())
		if err != nil {
			t.Error(err)
		}
		planned <- result
	}()
	select {
	case result := <-planned:
		if result.Drained != 0 {
			t.Fatalf("planner drained a container a claim holds")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("planner blocked on a claimed container")
	}
	if err := claim.Commit(t.Context()); err != nil {
		t.Fatal(err)
	}
	// One running task is demand for one container, so lift the floor to
	// show the running attempt alone keeps it.
	exec(t, pool, "update releases set spec = jsonb_set(spec, '{autoscaler,tasks_per_container}', '100') where id = $1", f.release)
	readyContainer(t, pool, f, host, time.Minute)
	plan(t, e)
	if state := containerState(t, pool, container); state != ContainerReady {
		t.Fatalf("claimed container is %s after the claim committed, want ready", state)
	}
}

// Claims race the planner on many idle containers. A claim inserts its
// attempt only while it holds a ready container FOR SHARE, so no draining
// container may ever carry a running attempt.
func TestDrainNeverOverlapsConcurrentClaims(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := newRelease(t, pool, `{"keep_warm_seconds": 0, "autoscaler": {"max_containers": 100, "tasks_per_container": 1000}}`)
	host := newHost(t, pool)
	containers := make([]uuid.UUID, 40)
	for i := range containers {
		containers[i] = readyContainer(t, pool, f, host, time.Minute)
	}

	ctx := t.Context()
	var wg sync.WaitGroup
	for worker := range 8 {
		wg.Go(func() {
			for i := worker; i < len(containers); i += 8 {
				err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
					var id uuid.UUID
					err := tx.QueryRow(ctx, "select id from containers where id = $1 and state = 'ready' for share", containers[i]).Scan(&id)
					if err != nil {
						return err //nolint:wrapcheck // pgx.ErrNoRows means the drain won.
					}
					if _, err := tx.Exec(ctx, "select pg_sleep(0.01)"); err != nil {
						return err //nolint:wrapcheck // Test helper.
					}
					attemptOn(t, tx, f, id, 0) //nolint:contextcheck // ctx is t.Context().
					return nil
				})
				if err != nil && err != pgx.ErrNoRows { //nolint:errorlint // pgx returns the sentinel unwrapped.
					t.Error(err)
				}
			}
		})
	}
	wg.Go(func() {
		for range 20 {
			if _, err := e.Plan(ctx, discardLogger()); err != nil {
				t.Error(err)
			}
		}
	})
	wg.Wait()

	var overlapping int
	if err := pool.QueryRow(ctx, `
select count(*) from containers c
where c.state = 'draining'
  and exists (select 1 from attempts a where a.container_id = c.id and a.state = 'running')`).Scan(&overlapping); err != nil {
		t.Fatal(err)
	}
	if overlapping != 0 {
		t.Fatalf("%d draining containers carry running attempts", overlapping)
	}
	states := containerStates(t, pool, f.release)
	if states[ContainerDraining] == 0 || states[ContainerReady] == 0 {
		t.Logf("states %v: the race did not split claims and drains this run", states)
	}
}

func TestStoppedWorkloadOrPausedAppWindsDown(t *testing.T) {
	for _, stop := range []string{
		"update workloads set desired_state = 'stopped' where id = $1",
		"update apps set state = 'paused' where id = (select app_id from workloads where id = $1)",
	} {
		pool := dbtest.New(t)
		e := NewExecution(pool)
		f := newRelease(t, pool, `{"autoscaler": {"max_containers": 5}}`)
		host := newHost(t, pool)
		busy := readyContainer(t, pool, f, host, time.Minute)
		attempt := attemptOn(t, pool, f, busy, 0)
		queueTasks(t, pool, f, 3, 0)
		plan(t, e) // requests pending containers for the queued tasks

		exec(t, pool, stop, f.workload)
		result := plan(t, e)
		states := containerStates(t, pool, f.release)
		if states[ContainerPending] != 0 || states[ContainerDraining] != 1 || states[ContainerReady] != 0 {
			t.Fatalf("%s: containers %v; want pending stopped and the busy one draining", stop, states)
		}
		var queued, cancelled int
		if err := pool.QueryRow(t.Context(), `
select count(*) filter (where status = 'queued'), count(*) filter (where status = 'cancelled')
from tasks where release_id = $1`, f.release).Scan(&queued, &cancelled); err != nil {
			t.Fatal(err)
		}
		if queued != 0 || cancelled != 3 || result.Cancelled != 3 {
			t.Fatalf("%s: %d queued, %d cancelled; want every queued task cancelled", stop, queued, cancelled)
		}
		var attemptState string
		if err := pool.QueryRow(t.Context(), "select state from attempts where id = $1", attempt).Scan(&attemptState); err != nil {
			t.Fatal(err)
		}
		if attemptState != string(AttemptRunning) {
			t.Fatalf("%s: running attempt became %s; it finishes on the draining container", stop, attemptState)
		}
	}
}
