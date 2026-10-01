package execution

import (
	"errors"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

const callbackSpec = `{"retry_policy": {"max_attempts": 2}, "callback_url": "https://hooks.example.com/t"}`

func containerOf(t *testing.T, pool *pgxpool.Pool, attempt AttemptID) ContainerID {
	t.Helper()
	var id uuid.UUID
	if err := pool.QueryRow(t.Context(), "select container_id from attempts where id = $1", uuid.UUID(attempt)).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return ContainerID(id)
}

func TestContainerPrincipalNeedsTheAssignedHostAndARunningTask(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	ctx := t.Context()
	e := NewExecution(pool)
	f := runningAttempt(t, pool, callbackSpec, 2)
	container := containerOf(t, pool, f.attempt)
	task := TaskID(f.task)

	p, err := e.ContainerPrincipal(ctx, f.host, container, &task)
	if err != nil {
		t.Fatal(err)
	}
	if p.Container != uuid.UUID(container) || *p.Attempt != uuid.UUID(f.attempt) || *p.RootTask != f.task || p.Workspace.Name == "" {
		t.Fatalf("principal %+v", p)
	}
	if _, err := e.ContainerPrincipal(ctx, compute.HostID(uuid.New()), container, nil); !errors.Is(err, ErrNotAssigned) {
		t.Fatalf("another host = %v", err)
	}
	other := TaskID(uuid.New())
	if _, err := e.ContainerPrincipal(ctx, f.host, container, &other); !errors.Is(err, ErrTaskNotRunningHere) {
		t.Fatalf("a task that does not run here = %v", err)
	}

	// Once the attempt ends, the task no longer speaks through the container.
	if err := e.CompleteAttempt(ctx, f.host, container, AttemptOutcome{
		Attempt: f.attempt, State: AttemptSucceeded, Result: &Payload{Encoding: EncodingJSON, Data: []byte("1")},
	}); err != nil {
		t.Fatal(err)
	}
	if _, err := e.ContainerPrincipal(ctx, f.host, container, &task); !errors.Is(err, ErrTaskNotRunningHere) {
		t.Fatalf("a finished task = %v", err)
	}
	if _, err := pool.Exec(ctx, "update containers set state = 'stopped', stopped_at = now()"); err != nil {
		t.Fatal(err)
	}
	if _, err := e.ContainerPrincipal(ctx, f.host, container, nil); !errors.Is(err, ErrNotAssigned) {
		t.Fatalf("a stopped container = %v", err)
	}
}

func TestSpawnedTasksRecordTheirLineage(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	ctx := t.Context()
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	root := submit(t, e, f, 1)[0]
	if root.Root != root.ID || root.Parent != nil {
		t.Fatalf("a root task = %+v", root)
	}
	children, err := e.Submit(ctx, SubmitRequest{
		Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: jsonInputs(2),
		Parent: &root.ID,
	})
	if err != nil {
		t.Fatal(err)
	}
	grandchild, err := e.Submit(ctx, SubmitRequest{
		Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: jsonInputs(1),
		Parent: &children[0].ID,
	})
	if err != nil {
		t.Fatal(err)
	}
	view, err := e.readTask(ctx, f.workspace, grandchild[0].ID)
	if err != nil {
		t.Fatal(err)
	}
	if *view.Parent != children[0].ID || view.Root != root.ID {
		t.Fatalf("grandchild lineage parent=%v root=%v", view.Parent, view.Root)
	}
}

func callbackEvents(t *testing.T, pool *pgxpool.Pool, task uuid.UUID) []string {
	t.Helper()
	rows, err := pool.Query(t.Context(), "select event || ':' || attempt from task_callbacks where task_id = $1 order by id", task)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var events []string
	for rows.Next() {
		var event string
		if err := rows.Scan(&event); err != nil {
			t.Fatal(err)
		}
		events = append(events, event)
	}
	return events
}

func TestTransitionsRecordCallbacksInTheirTransaction(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	ctx := t.Context()
	e := NewExecution(pool)

	// A retried failure, then a final one.
	f := runningAttempt(t, pool, callbackSpec, 2)
	failure := &Failure{Kind: FailureUserError, Type: "ValueError", Message: "nope"}
	if err := finish(ctx, e, &f.host, AttemptOutcome{Attempt: f.attempt, State: AttemptFailed, Failure: failure}); err != nil {
		t.Fatal(err)
	}
	if got := callbackEvents(t, pool, f.task); len(got) != 1 || got[0] != "retry:1" {
		t.Fatalf("after a retried failure: %v", got)
	}
	var stored []byte
	if err := pool.QueryRow(ctx, "select failure from task_callbacks where task_id = $1", f.task).Scan(&stored); err != nil || len(stored) == 0 {
		t.Fatalf("retry failure = %s, %v", stored, err)
	}

	// Cancelling a queued task reports it.
	if _, err := e.CancelTask(ctx, workspaceOf(t, pool, f.task), TaskID(f.task)); err != nil {
		t.Fatal(err)
	}
	if got := callbackEvents(t, pool, f.task); len(got) != 2 || got[1] != "cancelled:1" {
		t.Fatalf("after cancel: %v", got)
	}

	// A release without callback_url records nothing.
	quiet := runningAttempt(t, pool, `{}`, 1)
	if err := finish(ctx, e, &quiet.host, AttemptOutcome{
		Attempt: quiet.attempt, State: AttemptSucceeded, Result: &Payload{Encoding: EncodingJSON, Data: []byte("1")},
	}); err != nil {
		t.Fatal(err)
	}
	if got := callbackEvents(t, pool, quiet.task); len(got) != 0 {
		t.Fatalf("without callback_url: %v", got)
	}
}

func workspaceOf(t *testing.T, pool *pgxpool.Pool, task uuid.UUID) identity.WorkspaceID {
	t.Helper()
	var id uuid.UUID
	if err := pool.QueryRow(t.Context(), "select workspace_id from tasks where id = $1", task).Scan(&id); err != nil {
		t.Fatal(err)
	}
	return identity.WorkspaceID(id)
}

func TestStartFailedStopsOnlyAStartingContainerOfTheHost(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	ctx := t.Context()
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{}`)
	host, container := placedContainer(t, pool, f, ContainerStarting, 1)

	if err := e.StartFailed(ctx, compute.HostID(uuid.New()), container, "secret not found: X"); !errors.Is(err, ErrNotAssigned) {
		t.Fatalf("another host = %v", err)
	}
	if err := e.StartFailed(ctx, host, container, "secret not found: X"); err != nil {
		t.Fatal(err)
	}
	var state, reason, message string
	if err := pool.QueryRow(ctx, "select state, stop_reason, exit_message from containers where id = $1", uuid.UUID(container)).
		Scan(&state, &reason, &message); err != nil {
		t.Fatal(err)
	}
	if state != "stopped" || reason != "start_failed" || message != "secret not found: X" {
		t.Fatalf("container %s %s %q", state, reason, message)
	}
}
