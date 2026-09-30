package execution

import (
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func TestOverdueAttemptsRetryThenFailAsTimeouts(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	spec := `{"retry_policy": {"max_attempts": 2}}`
	retried := runningAttempt(t, pool, spec, 2)
	exhausted := runningAttempt(t, pool, spec, 1)
	onTime := runningAttempt(t, pool, spec, 2)
	exec(t, pool, "update attempts set deadline_at = now() - interval '1 second' where id = any($1)",
		[]uuid.UUID{uuid.UUID(retried.attempt), uuid.UUID(exhausted.attempt)})

	ended, err := e.TimeOutAttempts(t.Context(), discardLogger())
	if err != nil {
		t.Fatal(err)
	}
	if ended != 2 {
		t.Fatalf("ended %d attempts, want the 2 overdue ones", ended)
	}
	if status, _ := taskState(t, pool, retried.task); status != string(TaskQueued) {
		t.Fatalf("task with attempts left is %s, want queued for a retry", status)
	}
	if status, kind := taskState(t, pool, exhausted.task); status != string(TaskFailed) || kind == nil || *kind != string(FailureTimeout) {
		t.Fatalf("exhausted task is %s (%v), want failed timeout", status, kind)
	}
	if status, _ := taskState(t, pool, onTime.task); status != string(TaskRunning) {
		t.Fatalf("attempt within its deadline: task %s, want running", status)
	}
	var timedOut int
	if err := pool.QueryRow(t.Context(), "select count(*) from attempts where state = 'timed_out'").Scan(&timedOut); err != nil {
		t.Fatal(err)
	}
	if timedOut != 2 {
		t.Fatalf("%d timed_out attempts, want 2", timedOut)
	}
}

func TestLostHostStopsItsContainersAndRetriesAttempts(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	lost := runningAttempt(t, pool, `{"retry_policy": {"max_attempts": 2}}`, 2)
	alive := runningAttempt(t, pool, `{"retry_policy": {"max_attempts": 2}}`, 2)
	exec(t, pool, "update hosts set last_seen_at = now() - interval '1 minute' where id = $1", uuid.UUID(lost.host))
	exec(t, pool, "update hosts set last_seen_at = now() where id = $1", uuid.UUID(alive.host))

	n, err := e.ReleaseLostHosts(t.Context(), discardLogger())
	if err != nil {
		t.Fatal(err)
	}
	if n != 1 {
		t.Fatalf("marked %d hosts lost, want 1", n)
	}
	var hostState, ctrState, stopReason, attemptState string
	err = pool.QueryRow(t.Context(), `
select h.state, c.state, coalesce(c.stop_reason, ''), a.state
from attempts a join containers c on c.id = a.container_id join hosts h on h.id = c.host_id
where a.id = $1`, uuid.UUID(lost.attempt)).Scan(&hostState, &ctrState, &stopReason, &attemptState)
	if err != nil {
		t.Fatal(err)
	}
	if hostState != "lost" || ctrState != string(ContainerStopped) || stopReason != string(StopHostLost) || attemptState != string(AttemptLost) {
		t.Fatalf("host %s, container %s (%s), attempt %s; want lost, stopped (host_lost), lost", hostState, ctrState, stopReason, attemptState)
	}
	if status, _ := taskState(t, pool, lost.task); status != string(TaskQueued) {
		t.Fatalf("task on the lost host is %s, want queued for a retry", status)
	}
	if status, _ := taskState(t, pool, alive.task); status != string(TaskRunning) {
		t.Fatalf("task on a live host is %s, want running", status)
	}
}

func TestStuckStartsFailAfterTheStartTimeout(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := newRelease(t, pool, `{}`)
	host := newHost(t, pool)
	stuck := readyContainer(t, pool, f, host, 0)
	starting := readyContainer(t, pool, f, host, 0)
	exec(t, pool, "update containers set state = 'starting', ready_at = null, assigned_at = now() - interval '11 minutes' where id = $1", stuck)
	exec(t, pool, "update containers set state = 'starting', ready_at = null, assigned_at = now() - interval '1 minute' where id = $1", starting)

	n, err := e.TimeOutStarts(t.Context(), discardLogger())
	if err != nil {
		t.Fatal(err)
	}
	var reason string
	if err := pool.QueryRow(t.Context(), "select coalesce(stop_reason, '') from containers where id = $1", stuck).Scan(&reason); err != nil {
		t.Fatal(err)
	}
	if n != 1 || reason != string(StopStartFailed) {
		t.Fatalf("stopped %d, reason %q; want the stuck container stopped as start_failed", n, reason)
	}
	if state := containerState(t, pool, starting); state != ContainerStarting {
		t.Fatalf("container within the start timeout is %s, want starting", state)
	}
}
