package execution

import (
	"errors"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// finishTasks marks tasks succeeded with a JSON string result of size bytes
// each, or failed when size is negative.
func finishTasks(t *testing.T, pool *pgxpool.Pool, size int, tasks ...Task) {
	t.Helper()
	ids := make([]uuid.UUID, len(tasks))
	for n, task := range tasks {
		ids[n] = uuid.UUID(task.ID)
	}
	if size < 0 {
		exec(t, pool, `update tasks set status = 'failed', finished_at = now(),
failure = '{"kind": "user_error", "message": "no"}' where id = any($1)`, ids)
		return
	}
	exec(t, pool, "update tasks set status = 'succeeded', finished_at = now() where id = any($1)", ids)
	exec(t, pool, `insert into task_results (task_id, encoding, data)
select id, 'json', convert_to('"' || repeat('a', $2 - 2) || '"', 'UTF8') from unnest($1::uuid[]) id`, ids, size)
}

func taskIDs(tasks []Task) []TaskID {
	ids := make([]TaskID, len(tasks))
	for n, task := range tasks {
		ids[n] = task.ID
	}
	return ids
}

func TestWaitTasksReturnsOnlyFinishedTasksInRequestOrder(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	tasks := submit(t, e, f, 4)
	finishTasks(t, pool, 3, tasks[3], tasks[1])
	finishTasks(t, pool, -1, tasks[2])

	got, err := e.WaitTasks(t.Context(), l, f.workspace, taskIDs(tasks), 0)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 || got[0].Task.ID != tasks[1].ID || got[1].Task.ID != tasks[2].ID || got[2].Task.ID != tasks[3].ID {
		t.Fatalf("finished %+v, want tasks 1, 2 and 3", got)
	}
	if r := got[0].Result; r == nil || string(r.Data) != `"a"` || got[0].ResultOmitted {
		t.Fatalf("succeeded task %+v", got[0])
	}
	if got[1].Task.Status != TaskFailed || got[1].Task.Failure == nil || got[1].Result != nil || got[1].ResultOmitted {
		t.Fatalf("failed task %+v", got[1])
	}
}

func TestWaitTasksWakesOnAFinishDuringTheWait(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	tasks := submit(t, e, f, 3)
	host, container := placedContainer(t, pool, f, ContainerReady, 3)
	claimed, err := e.ClaimTasks(t.Context(), l, host, container, 3, 0)
	if err != nil || len(claimed) != 3 {
		t.Fatalf("claim: %v, %v", claimed, err)
	}
	last := claimed[len(claimed)-1]
	done := make(chan error, 1)
	go func() {
		time.Sleep(200 * time.Millisecond)
		done <- e.CompleteAttempt(t.Context(), host, container, AttemptOutcome{
			Attempt: last.Attempt, State: AttemptSucceeded, Result: &Payload{Encoding: EncodingJSON, Data: []byte("7")},
		})
	}()

	start := time.Now()
	got, err := e.WaitTasks(t.Context(), l, f.workspace, taskIDs(tasks), 30*time.Second)
	if err != nil || time.Since(start) > 5*time.Second {
		t.Fatalf("wait returned after %v: %v", time.Since(start), err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Task.ID != last.Task || got[0].Result == nil || string(got[0].Result.Data) != "7" {
		t.Fatalf("finished %+v, want the completed task with its result", got)
	}
}

func TestWaitTasksReturnsNoneAtTheDeadline(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	tasks := submit(t, e, f, 2)

	start := time.Now()
	got, err := e.WaitTasks(t.Context(), l, f.workspace, taskIDs(tasks), 300*time.Millisecond)
	if err != nil || got == nil || len(got) != 0 {
		t.Fatalf("got %+v, %v; want an empty list", got, err)
	}
	if waited := time.Since(start); waited < 300*time.Millisecond || waited > 5*time.Second {
		t.Fatalf("returned after %v, want the 300ms wait", waited)
	}
}

func TestWaitTasksRefusesTasksOutsideTheWorkspace(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	tasks := submit(t, e, f, 2)
	finishTasks(t, pool, 3, tasks...)
	var other uuid.UUID
	if err := pool.QueryRow(t.Context(), "insert into workspaces (name) values ('other') returning id").Scan(&other); err != nil {
		t.Fatal(err)
	}

	_, err := e.WaitTasks(t.Context(), l, identity.WorkspaceID(other), taskIDs(tasks), 0)
	var missing *TaskNotFoundError
	if !errors.As(err, &missing) || missing.Task != tasks[0].ID || !errors.Is(err, ErrNotFound) {
		t.Fatalf("other workspace: got %v, want task %s not found", err, tasks[0].ID)
	}
	unknown := TaskID(uuid.New())
	_, err = e.WaitTasks(t.Context(), l, f.workspace, []TaskID{tasks[0].ID, unknown, tasks[1].ID}, time.Minute)
	if !errors.As(err, &missing) || missing.Task != unknown {
		t.Fatalf("unknown id: got %v, want task %s not found", err, unknown)
	}
}

func TestWaitTasksInlinesResultsWithinTheBudget(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 100}`)
	tasks := submit(t, e, f, 19)
	finishTasks(t, pool, 3, tasks[0])
	// 200,000 bytes encode to more than 256 KiB of base64.
	exec(t, pool, "update tasks set status = 'succeeded', finished_at = now() where id = $1", uuid.UUID(tasks[1].ID))
	exec(t, pool, "insert into task_results (task_id, encoding, data) values ($1, 'cloudpickle', $2)",
		uuid.UUID(tasks[1].ID), make([]byte, 200_000))
	// Sixteen of these fit the 4 MiB of results; the seventeenth does not.
	finishTasks(t, pool, 250_000, tasks[2:]...)

	got, err := e.WaitTasks(t.Context(), l, f.workspace, taskIDs(tasks), 0)
	if err != nil || len(got) != len(tasks) {
		t.Fatalf("got %d tasks, %v", len(got), err)
	}
	for n, task := range got {
		omitted := n == 1 || n == len(tasks)-1
		if task.ResultOmitted != omitted || (task.Result == nil) != omitted {
			t.Fatalf("task %d: inline %v, omitted %v; want omitted %v", n, task.Result != nil, task.ResultOmitted, omitted)
		}
	}
	if len(got[2].Result.Data) != 250_000 {
		t.Fatalf("inline result has %d bytes", len(got[2].Result.Data))
	}
}
