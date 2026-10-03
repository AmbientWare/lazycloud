package execution

import (
	"context"
	"errors"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

type attemptFixture struct {
	task    uuid.UUID
	attempt AttemptID
	host    compute.HostID
}

// runningAttempt inserts a running task on a ready container. spec is the
// release spec JSON; maxAttempts is copied onto the task as admission does.
func runningAttempt(t *testing.T, pool *pgxpool.Pool, spec string, maxAttempts int) attemptFixture {
	t.Helper()
	ctx := t.Context()
	var f attemptFixture
	var hostID, attemptID uuid.UUID
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
select att.task_id, att.id, host.id from att, host`, spec, maxAttempts).Scan(&f.task, &attemptID, &hostID)
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
	return f
}

func finish(ctx context.Context, e *Execution, host *compute.HostID, outcome AttemptOutcome) error {
	return pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		stale, err := e.finishAttempts(ctx, tx, host, nil, []AttemptOutcome{outcome})
		if err == nil && stale[0] {
			err = ErrStaleAttempt
		}
		return err
	})
}

func taskState(t *testing.T, pool *pgxpool.Pool, task uuid.UUID) (status string, failure *string) {
	t.Helper()
	if err := pool.QueryRow(t.Context(), "select status, failure->>'kind' from tasks where id = $1", task).Scan(&status, &failure); err != nil {
		t.Fatal(err)
	}
	return status, failure
}

func TestRetryableFailureRequeuesUntilAttemptsRunOut(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	spec := `{"retry_policy": {"max_attempts": 2, "delay_seconds": 30}}`
	userError := &Failure{Kind: FailureUserError, Type: "ValueError", Message: "bad"}

	first := runningAttempt(t, pool, spec, 2)
	if err := finish(t.Context(), e, &first.host, AttemptOutcome{Attempt: first.attempt, State: AttemptFailed, Failure: userError}); err != nil {
		t.Fatal(err)
	}
	var status string
	var delayed bool
	if err := pool.QueryRow(t.Context(), "select status, available_at > now() + interval '25 seconds' from tasks where id = $1", first.task).Scan(&status, &delayed); err != nil {
		t.Fatal(err)
	}
	if status != string(TaskQueued) || !delayed {
		t.Fatalf("first failure: status %s, delayed %v; want queued after the retry delay", status, delayed)
	}

	last := runningAttempt(t, pool, spec, 1)
	if err := finish(t.Context(), e, &last.host, AttemptOutcome{Attempt: last.attempt, State: AttemptFailed, Failure: userError}); err != nil {
		t.Fatal(err)
	}
	if status, kind := taskState(t, pool, last.task); status != string(TaskFailed) || kind == nil || *kind != string(FailureUserError) {
		t.Fatalf("exhausted attempts: status %s, kind %v; want failed user_error", status, kind)
	}
}

// retry_on narrows which failures retry: here timeouts do, user errors
// fail the task at once.
func TestRetryOnRetriesOnlyTheNamedFailures(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	spec := `{"retry_policy": {"max_attempts": 3, "retry_on": ["timeout"]}}`

	timedOut := runningAttempt(t, pool, spec, 3)
	if err := finish(t.Context(), e, &timedOut.host, AttemptOutcome{
		Attempt: timedOut.attempt, State: AttemptTimedOut, Failure: &Failure{Kind: FailureTimeout, Message: "ran past 60s"},
	}); err != nil {
		t.Fatal(err)
	}
	if status, _ := taskState(t, pool, timedOut.task); status != string(TaskQueued) {
		t.Fatalf("timeout: status %s, want queued for another attempt", status)
	}

	failed := runningAttempt(t, pool, spec, 3)
	if err := finish(t.Context(), e, &failed.host, AttemptOutcome{
		Attempt: failed.attempt, State: AttemptFailed, Failure: &Failure{Kind: FailureUserError, Type: "ValueError", Message: "bad"},
	}); err != nil {
		t.Fatal(err)
	}
	if status, kind := taskState(t, pool, failed.task); status != string(TaskFailed) || kind == nil || *kind != string(FailureUserError) {
		t.Fatalf("user error: status %s, kind %v; want failed with attempts left", status, kind)
	}
}

func TestLoadErrorIsTerminal(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := runningAttempt(t, pool, `{"retry_policy": {"max_attempts": 5}}`, 5)
	err := finish(t.Context(), e, nil, AttemptOutcome{
		Attempt: f.attempt, State: AttemptFailed, Failure: &Failure{Kind: FailureLoadError, Message: "no module"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if status, _ := taskState(t, pool, f.task); status != string(TaskFailed) {
		t.Fatalf("status %s, want failed", status)
	}
}

func TestCompletionFromAnotherHostIsStale(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := runningAttempt(t, pool, `{}`, 1)
	other := compute.HostID(uuid.New())
	result := &Payload{Encoding: EncodingJSON, Data: []byte(`1`)}
	err := finish(t.Context(), e, &other, AttemptOutcome{Attempt: f.attempt, State: AttemptSucceeded, Result: result})
	if !errors.Is(err, ErrStaleAttempt) {
		t.Fatalf("got %v, want ErrStaleAttempt", err)
	}
	if err := finish(t.Context(), e, &f.host, AttemptOutcome{Attempt: f.attempt, State: AttemptSucceeded, Result: result}); err != nil {
		t.Fatal(err)
	}
	if status, _ := taskState(t, pool, f.task); status != string(TaskSucceeded) {
		t.Fatalf("status %s, want succeeded", status)
	}
	// The attempt is finished, so a duplicate delivery is stale too.
	err = finish(t.Context(), e, &f.host, AttemptOutcome{Attempt: f.attempt, State: AttemptSucceeded, Result: result})
	if !errors.Is(err, ErrStaleAttempt) {
		t.Fatalf("duplicate completion: got %v, want ErrStaleAttempt", err)
	}
}
