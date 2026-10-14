package execution

import (
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

type attemptFixture struct {
	task      uuid.UUID
	attempt   AttemptID
	host      compute.HostID
	container ContainerID
}

// runningAttempt inserts a running task on a ready container. spec is the
// release spec JSON; maxAttempts is copied onto the task as admission does.
func runningAttempt(t *testing.T, pool *pgxpool.Pool, spec string, maxAttempts int) attemptFixture {
	t.Helper()
	ctx := t.Context()
	var f attemptFixture
	var hostID, attemptID, containerID uuid.UUID
	err := pgx.BeginFunc(ctx, pool, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, `
with ws as (insert into workspaces (name) values ('ws-' || substr(md5(random()::text), 1, 8)) returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, $1::jsonb, sha256('spec'), sha256('src') from wl returning id, workload_id),
     host as (insert into hosts (name, token_hash, state, cpu_millis, memory_bytes)
              values ('h', sha256(random()::text::bytea), 'online', 4000, 1 << 32) returning id),
     ctr as (insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes)
             select ws.id, rel.id, 'ready', host.id, 1, 1000, 1 << 28 from ws, rel, host returning id),
     task as (insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts, started_at)
              select ws.id, rel.workload_id, rel.id, 'running', 1, $2, now() from ws, rel returning id),
     att as (insert into attempts (task_id, number, container_id, state, deadline_at)
             select task.id, 1, ctr.id, 'running', now() + interval '1 hour' from task, ctr returning id, task_id)
select att.task_id, att.id, host.id, ctr.id from att, host, ctr`, spec, maxAttempts).Scan(&f.task, &attemptID, &hostID, &containerID)
	})
	if err != nil {
		t.Fatalf("insert fixture: %v", err)
	}
	dbtest.OwnWorkspaces(t, pool)
	if _, err := pool.Exec(ctx, "update tasks set current_attempt_id = $1 where id = $2", attemptID, f.task); err != nil {
		t.Fatal(err)
	}
	f.attempt = AttemptID(attemptID)
	f.host = compute.HostID(hostID)
	f.container = ContainerID(containerID)
	return f
}

// finish reports how f's running attempt ended, as its host does.
func finish(t *testing.T, e *Execution, f attemptFixture, state AttemptState, failure *Failure) {
	t.Helper()
	outcome := AttemptOutcome{Attempt: f.attempt, State: state, Failure: failure}
	if state == AttemptSucceeded {
		outcome.Result = &Payload{Encoding: EncodingJSON, Data: []byte("1")}
	}
	if err := e.CompleteAttempt(t.Context(), f.host, f.container, outcome); err != nil {
		t.Fatal(err)
	}
}

func taskState(t *testing.T, pool *pgxpool.Pool, task uuid.UUID) (status string, failure *string) {
	t.Helper()
	if err := pool.QueryRow(t.Context(), "select status, failure->>'kind' from tasks where id = $1", task).Scan(&status, &failure); err != nil {
		t.Fatal(err)
	}
	return status, failure
}

// A failed attempt requeues its task, after the policy's delay, while the
// policy has attempts left and retries that kind of failure. A load error,
// or a failure retry_on leaves out, fails the task at once. A slot that
// crashed is a user error.
func TestFailedAttemptsRetryByPolicy(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	e := NewExecution(pool)
	userError := &Failure{Kind: FailureUserError, Type: "ValueError", Message: "bad"}
	crash := CrashFailure("")
	const (
		delayed   = `{"retry_policy": {"max_attempts": 2, "delay_seconds": 30}}`
		onTimeout = `{"retry_policy": {"max_attempts": 3, "retry_on": ["timeout"]}}`
	)
	for _, c := range []struct {
		name        string
		spec        string
		maxAttempts int
		state       AttemptState
		failure     *Failure
		want        TaskStatus
	}{
		{"a user error with attempts left", delayed, 2, AttemptFailed, userError, TaskQueued},
		{"a user error on the last attempt", delayed, 1, AttemptFailed, userError, TaskFailed},
		{"a crashed slot with attempts left", delayed, 2, AttemptFailed, &crash, TaskQueued},
		{"a timeout retry_on names", onTimeout, 3, AttemptTimedOut, &Failure{Kind: FailureTimeout, Message: "ran past 60s"}, TaskQueued},
		{"a user error retry_on leaves out", onTimeout, 3, AttemptFailed, userError, TaskFailed},
		{"a load error", `{"retry_policy": {"max_attempts": 5}}`, 5, AttemptFailed, &Failure{Kind: FailureLoadError, Message: "no module"}, TaskFailed},
	} {
		f := runningAttempt(t, pool, c.spec, c.maxAttempts)
		finish(t, e, f, c.state, c.failure)
		var status string
		var kind *string
		var waits bool
		if err := pool.QueryRow(t.Context(), "select status, failure->>'kind', available_at > now() + interval '25 seconds' from tasks where id = $1", f.task).
			Scan(&status, &kind, &waits); err != nil {
			t.Fatal(err)
		}
		switch {
		case status != string(c.want):
			t.Errorf("%s: task %s, want %s", c.name, status, c.want)
		case c.want == TaskFailed && (kind == nil || *kind != string(c.failure.Kind)):
			t.Errorf("%s: failed with %v, want %s", c.name, kind, c.failure.Kind)
		case c.want == TaskQueued && c.spec == delayed && !waits:
			t.Errorf("%s: requeued before the retry delay", c.name)
		}
	}
}
