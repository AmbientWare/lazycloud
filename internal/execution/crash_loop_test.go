package execution

import (
	"log/slog"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// startOnHost moves the pod's pending serve containers to starting on host.
func startOnHost(t *testing.T, e *Execution, f podFixture, host uuid.UUID) []ContainerID {
	t.Helper()
	rows, err := e.pool.Query(t.Context(), `update containers c set state = 'starting', host_id = $2, assigned_at = now()
from releases r where r.id = c.release_id and r.workload_id = $1 and c.state = 'pending' returning c.id`, f.workload, host)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var out []ContainerID
	for rows.Next() {
		var id uuid.UUID
		if err := rows.Scan(&id); err != nil {
			t.Fatal(err)
		}
		out = append(out, ContainerID(id))
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return out
}

func exited(t *testing.T, e *Execution, host uuid.UUID, container ContainerID, exit ContainerExit) {
	t.Helper()
	if _, err := e.ApplyReport(t.Context(), compute.HostID(host), ContainerReport{Container: container, Phase: ReportExited, Exit: &exit}); err != nil {
		t.Fatal(err)
	}
}

func planPods(t *testing.T, e *Execution) PlanResult {
	t.Helper()
	result, err := e.PlanPods(t.Context(), slog.New(slog.DiscardHandler))
	if err != nil {
		t.Fatal(err)
	}
	return result
}

// A container that exits before it was ever ready failed to start, whatever
// ended it, and counts toward the limit; at the limit queued tasks fail and
// the release keeps no warm minimum. A stop by request or an exit after
// ready does not count.
func TestExitsBeforeReadyCountAsStartFailures(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"resources": {"cpu_millis": 500, "memory_mib": 256}, "max_pending_tasks": 10,
 "autoscaler": {"min_containers": 1, "max_containers": 1}}`)
	failures := func() int {
		t.Helper()
		var n int
		if err := pool.QueryRow(t.Context(), "select start_failures from releases where id = $1", f.release).Scan(&n); err != nil {
			t.Fatal(err)
		}
		return n
	}
	task := submit(t, e, f, 1)[0]

	host, ready := placedContainer(t, pool, f, ContainerReady, 1)
	exited(t, e, uuid.UUID(host), ready, ContainerExit{Reason: StopCrashed})
	host, stopped := placedContainer(t, pool, f, ContainerStarting, 1)
	exited(t, e, uuid.UUID(host), stopped, ContainerExit{Reason: StopRequested})
	if n := failures(); n != 0 {
		t.Fatalf("a crash after ready and a requested stop counted %d start failures", n)
	}

	code := 3
	for n, exit := range []ContainerExit{
		{Reason: StopCrashed, Message: "exit code 3: seed the devbox root: no space left on device"},
		{Reason: StopExited, ExitCode: &code},
		{Reason: StopOutOfMemory},
	} {
		host, container := placedContainer(t, pool, f, ContainerStarting, 1)
		exited(t, e, uuid.UUID(host), container, exit)
		if got := failures(); got != n+1 {
			t.Fatalf("after a %s exit before ready, start failures = %d, want %d", exit.Reason, got, n+1)
		}
	}
	var kind string
	if err := pool.QueryRow(t.Context(), "select coalesce(failure ->> 'kind', '') from tasks where id = $1", uuid.UUID(task.ID)).Scan(&kind); err != nil {
		t.Fatal(err)
	}
	if status(t, pool, task.ID) != TaskFailed || kind != string(FailureStartFailed) {
		t.Fatalf("at the limit the queued task is %s (%q), want failed as start_failed", status(t, pool, task.ID), kind)
	}
	// Past any retry delay, the warm minimum asks for nothing.
	exec(t, pool, "update containers set stopped_at = now() - interval '1 hour'")
	if created := plan(t, e).Created; created != 0 {
		t.Fatalf("a release at the start failure limit started %d containers", created)
	}
}

// After a failed start the next container waits, longer after each
// consecutive failure, and the plan names when it may start.
func TestFailedStartsBackOff(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedPod(t, pool, "pod", 600)
	host := newHost(t, pool)
	if _, err := e.WakePod(t.Context(), f.workspace, f.workload); err != nil {
		t.Fatal(err)
	}

	for n, delay := range []time.Duration{startRetryBase, 2 * startRetryBase} {
		if created := planPods(t, e).Created; created != 1 {
			t.Fatalf("attempt %d: planning started %d containers, want 1", n+1, created)
		}
		for _, c := range startOnHost(t, e, f, host) {
			exited(t, e, host, c, ContainerExit{Reason: StopCrashed, Message: "exit code 3"})
		}
		result := planPods(t, e)
		if wait := time.Until(result.RetryAt); result.Created != 0 || wait <= delay-time.Second || wait > delay {
			t.Fatalf("after failure %d: started %d, retry in %s; want none for %s", n+1, result.Created, wait, delay)
		}
		exec(t, pool, "update containers set stopped_at = stopped_at - make_interval(secs => $1)", delay.Seconds())
	}

	// Functions wait the same way.
	fn := newRelease(t, pool, `{}`)
	queueTasks(t, pool, fn, 1, 0)
	container := readyContainer(t, pool, fn, host, 0)
	exec(t, pool, "update containers set state = 'starting', ready_at = null where id = $1", container)
	exited(t, e, host, ContainerID(container), ContainerExit{Reason: StopCrashed})
	if result := plan(t, e); result.Created != 0 || result.RetryAt.IsZero() {
		t.Fatalf("a function retried a failed start at once: %+v", result)
	}
}

// A stop holds a pod down whatever it is doing: a pending container stops,
// a starting one drains and its host is told to stop it, and neither an
// earlier scale nor always-on starts another until the next wake.
func TestParkStopsStartingAndCrashLoopingPods(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedPod(t, pool, "devbox", -1)
	host := newHost(t, pool)
	if err := e.ScalePod(t.Context(), f.workspace, f.workload, 2); err != nil {
		t.Fatal(err)
	}
	if created := planPods(t, e).Created; created != 2 {
		t.Fatalf("a pod scaled to 2 started %d containers", created)
	}
	exec(t, pool, `update containers set state = 'starting', host_id = $1, assigned_at = now()
where id = (select id from containers where state = 'pending' order by id limit 1)`, host)

	// A crash-looping pod: an earlier start just failed.
	failed := podContainerOn(t, pool, f, host)
	exited(t, e, host, failed, ContainerExit{Reason: StopCrashed, Message: "exit code 3"})

	if err := e.ParkPod(t.Context(), f.workspace, f.workload); err != nil {
		t.Fatal(err)
	}
	states := containerStates(t, pool, f.release)
	if states[ContainerPending] != 0 || states[ContainerStarting] != 0 || states[ContainerDraining] != 1 {
		t.Fatalf("after a stop the containers are %v; want the starting one draining and none pending", states)
	}
	commands, err := e.HostCommands(t.Context(), compute.HostID(host))
	if err != nil || len(commands.Stop) != 1 || len(commands.Start) != 0 {
		t.Fatalf("host commands after a stop: %+v, %v; want one stop", commands, err)
	}
	exec(t, pool, "update containers set stopped_at = now() - interval '1 hour' where state = 'stopped'")
	if created := planPods(t, e).Created; created != 0 {
		t.Fatalf("a stopped pod started %d containers", created)
	}
	if _, err := e.WakePod(t.Context(), f.workspace, f.workload); err != nil {
		t.Fatal(err)
	}
	if created := planPods(t, e).Created; created != 1 {
		t.Fatalf("a woken always-on pod started %d containers, want 1", created)
	}
}

// podContainerOn inserts a starting serve container of the pod on host.
func podContainerOn(t *testing.T, pool *pgxpool.Pool, f podFixture, host uuid.UUID) ContainerID {
	t.Helper()
	var id uuid.UUID
	if err := pool.QueryRow(t.Context(), `insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
values ($1, $2, 'starting', $3, 1, 500, 1 << 28, now()) returning id`, uuid.UUID(f.workspace), f.release, host).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return ContainerID(id)
}

// A task submitted to a release that stopped at the start failure limit
// gives it one fresh start at once. When that start fails the task fails
// with its error, and a task submitted within the backoff after it fails
// with that error without another start.
func TestATaskRetriesAReleaseThatStoppedStarting(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"resources": {"cpu_millis": 500, "memory_mib": 256}, "max_pending_tasks": 10}`)
	host := newHost(t, pool)
	_, failed := placedContainer(t, pool, f, ContainerStopped, 1)
	exec(t, pool, `update containers set stop_reason = 'start_failed', exit_message = 'pull image: not found',
stopped_at = now() - interval '1 hour' where id = $1`, uuid.UUID(failed))
	exec(t, pool, "update releases set start_failures = $2 where id = $1", f.release, startFailureLimit)

	first := submit(t, e, f, 1)[0]
	if first.Status != TaskQueued {
		t.Fatalf("a task for a release past its backoff is %s, want queued", first.Status)
	}
	if created := plan(t, e).Created; created != 1 {
		t.Fatalf("the retried release started %d containers, want 1 at once", created)
	}
	exec(t, pool, "update containers set state = 'starting', host_id = $2, assigned_at = now() where release_id = $1 and state = 'pending'", f.release, host)
	var fresh uuid.UUID
	if err := pool.QueryRow(t.Context(), "select id from containers where release_id = $1 and state = 'starting'", f.release).Scan(&fresh); err != nil {
		t.Fatal(err)
	}
	const cause = "exit code 1: secret DATABASE_URL is not set"
	exited(t, e, host, ContainerID(fresh), ContainerExit{Reason: StopCrashed, Message: cause})
	task, err := e.readTask(t.Context(), f.workspace, first.ID)
	if err != nil || task.Status != TaskFailed || task.Failure == nil || task.Failure.Kind != FailureStartFailed || task.Failure.Message != cause {
		t.Fatalf("the task whose fresh start failed: %+v, %v", task.Failure, err)
	}

	second := submit(t, e, f, 1)[0]
	if second.Status != TaskFailed || second.Failure == nil || second.Failure.Message != cause {
		t.Fatalf("a task within the backoff: %s %+v, want failed with %q", second.Status, second.Failure, cause)
	}
	if created := plan(t, e).Created; created != 0 {
		t.Fatalf("a task within the backoff started %d containers", created)
	}
}
