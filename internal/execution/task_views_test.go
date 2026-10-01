package execution

import (
	"errors"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func pendingReason(t *testing.T, e *Execution, f functionFixture, id TaskID) PendingReason {
	t.Helper()
	task, err := e.readTask(t.Context(), f.workspace, id)
	if err != nil {
		t.Fatal(err)
	}
	if task.Pending == nil {
		return ""
	}
	return task.Pending.Reason
}

func TestPendingReasonFollowsTheReleaseContainers(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	task := submit(t, e, f, 1)[0]
	if got := pendingReason(t, e, f, task.ID); got != PendingQueued {
		t.Fatalf("without containers %s, want queued", got)
	}

	exec(t, pool, `insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes)
	               values ($1, $2, 'pending', 1, 1000, 1 << 28)`, uuid.UUID(f.workspace), f.release)
	if got := pendingReason(t, e, f, task.ID); got != PendingCapacityUnavailable {
		t.Fatalf("with an unplaced container %s, want capacity_unavailable", got)
	}

	placedContainer(t, pool, f, ContainerStarting, 1)
	if got := pendingReason(t, e, f, task.ID); got != PendingStartingContainer {
		t.Fatalf("with a starting container %s, want starting_container", got)
	}

	exec(t, pool, `delete from containers where state = 'pending'`)
	exec(t, pool, `update containers set state = 'ready', ready_at = now() where release_id = $1`, f.release)
	if got := pendingReason(t, e, f, task.ID); got != PendingCapacityBusy {
		t.Fatalf("with ready containers %s, want capacity_busy", got)
	}

	exec(t, pool, `update tasks set attempt_count = 1, available_at = now() + interval '1 hour' where id = $1`, uuid.UUID(task.ID))
	view, err := e.readTask(t.Context(), f.workspace, task.ID)
	if err != nil {
		t.Fatal(err)
	}
	if view.Pending == nil || view.Pending.Reason != PendingRetry || view.NextAttemptAt == nil {
		t.Fatalf("waiting to retry %+v next %v, want retry", view.Pending, view.NextAttemptAt)
	}
	if !view.Pending.PendingSince.Equal(view.CreatedAt) {
		t.Fatalf("pending since %v, want the submit time %v", view.Pending.PendingSince, view.CreatedAt)
	}

	exec(t, pool, `update tasks set status = 'running' where id = $1`, uuid.UUID(task.ID))
	if got := pendingReason(t, e, f, task.ID); got != "" {
		t.Fatalf("running task pending %s, want none", got)
	}
}

func TestListTasksPagesNewestFirstWithFilters(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	tasks := submit(t, e, f, 5)
	if _, err := e.CancelTask(t.Context(), f.workspace, tasks[0].ID); err != nil {
		t.Fatal(err)
	}

	var seen []TaskID
	cursor := ""
	for range 3 {
		page, err := e.ListTasks(t.Context(), f.workspace, TaskFilter{}, 2, cursor)
		if err != nil {
			t.Fatal(err)
		}
		for _, task := range page.Tasks {
			seen = append(seen, task.ID)
		}
		if cursor = page.Next; cursor == "" {
			break
		}
	}
	if len(seen) != 5 || seen[0] != tasks[4].ID || seen[4] != tasks[0].ID {
		t.Fatalf("paged tasks %v, want all five newest first", seen)
	}

	app := "reports"
	fn := "summarize"
	cancelled := TaskCancelled
	page, err := e.ListTasks(t.Context(), f.workspace, TaskFilter{App: &app, Function: &fn, Status: &cancelled}, 10, "")
	if err != nil || len(page.Tasks) != 1 || page.Tasks[0].ID != tasks[0].ID {
		t.Fatalf("filtered tasks %+v %v, want the cancelled one", page.Tasks, err)
	}
	queued := TaskQueued
	page, err = e.ListTasks(t.Context(), f.workspace, TaskFilter{App: &app, Status: &queued}, 10, "")
	if err != nil || len(page.Tasks) != 4 || page.Tasks[0].Pending == nil {
		t.Fatalf("queued app tasks %d %v, want four with pending progress", len(page.Tasks), err)
	}
	other := "other"
	if page, err := e.ListTasks(t.Context(), f.workspace, TaskFilter{App: &other}, 10, ""); err != nil || len(page.Tasks) != 0 {
		t.Fatalf("unknown app tasks %+v %v", page.Tasks, err)
	}
	if _, err := e.ListTasks(t.Context(), f.workspace, TaskFilter{Function: &fn}, 10, ""); !errors.Is(err, ErrInvalidFilter) {
		t.Fatalf("function without app error %v", err)
	}
	if _, err := e.ListTasks(t.Context(), f.workspace, TaskFilter{}, 10, "nope"); !errors.Is(err, ErrInvalidCursor) {
		t.Fatalf("bad cursor error %v", err)
	}

	// Search matches an id prefix or the function name on the server, and
	// a LIKE wildcard in it is a literal character.
	prefix := tasks[2].ID.String()[:35]
	for _, filter := range []TaskFilter{{Search: &prefix}, {App: &app, Search: &prefix}} {
		page, err := e.ListTasks(t.Context(), f.workspace, filter, 10, "")
		if err != nil || len(page.Tasks) != 1 || page.Tasks[0].ID != tasks[2].ID {
			t.Fatalf("id prefix search %+v: %+v %v", filter, page.Tasks, err)
		}
	}
	name, wildcard := "SUMMAR", "%"
	if page, err := e.ListTasks(t.Context(), f.workspace, TaskFilter{Search: &name}, 10, ""); err != nil || len(page.Tasks) != 5 {
		t.Fatalf("function search %d %v, want all five", len(page.Tasks), err)
	}
	if page, err := e.ListTasks(t.Context(), f.workspace, TaskFilter{Search: &wildcard}, 10, ""); err != nil || len(page.Tasks) != 0 {
		t.Fatalf("wildcard search %d %v, want none", len(page.Tasks), err)
	}

	// A task another task spawned is left out of a root-only listing.
	exec(t, pool, `update tasks set parent_task_id = $1 where id = $2`, uuid.UUID(tasks[0].ID), uuid.UUID(tasks[1].ID))
	for _, filter := range []TaskFilter{{RootOnly: true}, {App: &app, RootOnly: true}} {
		page, err := e.ListTasks(t.Context(), f.workspace, filter, 10, "")
		if err != nil || len(page.Tasks) != 4 {
			t.Fatalf("root-only tasks %+v: %d %v, want four", filter, len(page.Tasks), err)
		}
		for _, task := range page.Tasks {
			if task.ID == tasks[1].ID {
				t.Fatalf("root-only listing has the spawned task")
			}
		}
	}
}

func TestStopTasksCancelsLiveTasksAndSkipsTheRest(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	host, container := placedContainer(t, pool, f, ContainerReady, 1)
	tasks := submit(t, e, f, 3)
	claimed, err := e.ClaimTasks(t.Context(), l, host, container, 1, 0)
	if err != nil || len(claimed) != 1 {
		t.Fatalf("claim %v %v", claimed, err)
	}
	if _, err := e.CancelTask(t.Context(), f.workspace, tasks[2].ID); err != nil {
		t.Fatal(err)
	}
	unknown := TaskID(uuid.New())
	result, err := e.StopTasks(t.Context(), f.workspace, []TaskID{tasks[0].ID, tasks[1].ID, tasks[2].ID, unknown})
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Stopped) != 2 || len(result.Skipped) != 2 || result.Skipped[1] != unknown {
		t.Fatalf("stop result %+v", result)
	}
	var attempt string
	if err := pool.QueryRow(t.Context(), "select state from attempts where id = $1", uuid.UUID(claimed[0].Attempt)).Scan(&attempt); err != nil || attempt != "cancelled" {
		t.Fatalf("running attempt %s %v, want cancelled", attempt, err)
	}
}

func TestRerunSubmitsTheSameInputToTheSameRelease(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	upstream := submit(t, e, f, 1)[0]
	original := submitInputs(t, e, f, dependentInput(upstream.ID))[0]
	rerun, err := e.RerunTask(t.Context(), f.workspace, original.ID)
	if err != nil {
		t.Fatal(err)
	}
	if rerun.ID == original.ID || rerun.Release != f.release || pendingReason(t, e, f, rerun.ID) != PendingDependencies {
		t.Fatalf("rerun %+v, want a new task on the release waiting on the same upstream", rerun)
	}
	var same bool
	if err := pool.QueryRow(t.Context(), `select a.data = b.data from task_inputs a, task_inputs b
	                                       where a.task_id = $1 and b.task_id = $2`, uuid.UUID(original.ID), uuid.UUID(rerun.ID)).Scan(&same); err != nil || !same {
		t.Fatalf("rerun input matches %v %v", same, err)
	}
	if _, err := e.RerunTask(t.Context(), f.workspace, TaskID(uuid.New())); !errors.Is(err, ErrNotFound) {
		t.Fatalf("rerun of unknown task %v", err)
	}
}

// Lineage itself is proven in workload_runtime_test.go.
func TestSubmitRejectsAParentOutsideTheWorkspace(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	missing := TaskID(uuid.New())
	_, err := e.Submit(t.Context(), SubmitRequest{
		Workspace: f.workspace, App: "reports", Function: "summarize", Parent: &missing, Inputs: jsonInputs(1),
	})
	var unknown *UnknownTaskError
	if !errors.As(err, &unknown) {
		t.Fatalf("unknown parent error %v", err)
	}
}

func TestStopContainerLosesItsAttemptsForRetry(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10, "retry_policy": {"max_attempts": 2}}`)
	host, container := placedContainer(t, pool, f, ContainerReady, 1)
	task := submit(t, e, f, 1)[0]
	if claimed, err := e.ClaimTasks(t.Context(), l, host, container, 1, 0); err != nil || len(claimed) != 1 {
		t.Fatalf("claim %v %v", claimed, err)
	}
	stopped, err := e.StopContainer(t.Context(), f.workspace, container)
	if err != nil {
		t.Fatal(err)
	}
	if stopped.State != ContainerDraining || stopped.RunningTasks != 0 {
		t.Fatalf("stopped container %s with %d running, want draining with none", stopped.State, stopped.RunningTasks)
	}
	if s := status(t, pool, task.ID); s != TaskQueued {
		t.Fatalf("task after container stop %s, want queued for retry", s)
	}
	commands, err := e.HostCommands(t.Context(), host)
	if err != nil || len(commands.Stop) != 1 || commands.Stop[0].Container != container {
		t.Fatalf("host commands %+v %v, want a stop", commands, err)
	}
	if _, err := e.StopContainer(t.Context(), f.workspace, ContainerID(uuid.New())); !errors.Is(err, ErrNotFound) {
		t.Fatalf("stop of unknown container %v", err)
	}

	page, err := e.ListContainers(t.Context(), f.workspace, ContainerFilter{Live: true}, 10, "")
	if err != nil || len(page.Containers) != 1 || page.Containers[0].Function != "summarize" {
		t.Fatalf("live containers %+v %v", page.Containers, err)
	}
	app, other := "reports", "other"
	if page, err := e.ListContainers(t.Context(), f.workspace, ContainerFilter{App: &app}, 10, ""); err != nil || len(page.Containers) != 1 {
		t.Fatalf("app containers %+v %v", page.Containers, err)
	}
	if page, err := e.ListContainers(t.Context(), f.workspace, ContainerFilter{App: &other}, 10, ""); err != nil || len(page.Containers) != 0 {
		t.Fatalf("another app's containers %+v %v", page.Containers, err)
	}
	// A container shows the image its release runs.
	exec(t, pool, `update releases set spec = spec || '{"image": {"python_version": "3.12", "reference": "registry.example/fn@sha256:ab"}}'`)
	if c, err := e.GetContainer(t.Context(), f.workspace, container); err != nil || c.Image != "registry.example/fn@sha256:ab" {
		t.Fatalf("container image %q %v", c.Image, err)
	}
}

func TestDeletedWorkloadCancelsRunningAndQueuedTasks(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10, "autoscaler": {"max_containers": 1}, "resources": {"cpu_millis": 1000, "memory_mib": 256}}`)
	host, container := placedContainer(t, pool, f, ContainerReady, 1)
	tasks := submit(t, e, f, 2)
	if claimed, err := e.ClaimTasks(t.Context(), l, host, container, 1, 0); err != nil || len(claimed) != 1 {
		t.Fatalf("claim %v %v", claimed, err)
	}

	// A stopped workload lets its running task finish.
	exec(t, pool, `update workloads set desired_state = 'stopped'`)
	if _, err := e.Plan(t.Context(), discardLogger()); err != nil {
		t.Fatal(err)
	}
	if status(t, pool, tasks[0].ID) != TaskRunning || status(t, pool, tasks[1].ID) != TaskCancelled {
		t.Fatalf("stopped workload tasks %s %s, want running and cancelled", status(t, pool, tasks[0].ID), status(t, pool, tasks[1].ID))
	}

	exec(t, pool, `update workloads set desired_state = 'deleted', deleted_at = now()`)
	if _, err := e.Plan(t.Context(), discardLogger()); err != nil {
		t.Fatal(err)
	}
	if s := status(t, pool, tasks[0].ID); s != TaskCancelled {
		t.Fatalf("deleted workload's running task %s, want cancelled", s)
	}
	commands, err := e.HostCommands(t.Context(), host)
	if err != nil || len(commands.Stop) != 1 || len(commands.Cancel) != 1 {
		t.Fatalf("host commands %+v %v, want the stop and the slot kill", commands, err)
	}
}

func TestWorkingTreeReleaseRunsWhileTheDeploymentIsStopped(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10, "autoscaler": {"max_containers": 1}, "resources": {"cpu_millis": 1000, "memory_mib": 256}}`)
	var run uuid.UUID
	if err := pool.QueryRow(t.Context(), `insert into releases (workload_id, spec, spec_digest, source_sha256)
	    select workload_id, spec, sha256('run'), source_sha256 from releases where id = $1 returning id`, f.release).Scan(&run); err != nil {
		t.Fatal(err)
	}
	exec(t, pool, `update workloads set desired_state = 'stopped'`)
	if _, err := e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: jsonInputs(1)}); !errors.Is(err, ErrNotAccepting) {
		t.Fatalf("deployed submit to a stopped workload %v", err)
	}
	if _, err := e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "summarize", Release: &f.release, Inputs: jsonInputs(1)}); !errors.Is(err, ErrNotAccepting) {
		t.Fatalf("submit to a stopped deployed version %v", err)
	}
	tasks, err := e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "summarize", Release: &run, Inputs: jsonInputs(1)})
	if err != nil || tasks[0].Release != run || tasks[0].Version != nil {
		t.Fatalf("working-tree submit %+v %v", tasks, err)
	}
	result, err := e.Plan(t.Context(), discardLogger())
	if err != nil || result.Created != 1 || result.Cancelled != 0 {
		t.Fatalf("plan %+v %v, want one container for the working-tree release", result, err)
	}
	exec(t, pool, `update apps set state = 'paused'`)
	if _, err := e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "summarize", Release: &run, Inputs: jsonInputs(1)}); !errors.Is(err, ErrNotAccepting) {
		t.Fatalf("working-tree submit to a paused app %v", err)
	}
}

// A line whose transaction commits after a later line is still delivered:
// readers hold back lines that an older running transaction may precede.
func TestLogReadersNeverSkipLinesThatCommitOutOfOrder(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	host, container := placedContainer(t, pool, f, ContainerReady, 2)
	submit(t, e, f, 2)
	claimed, err := e.ClaimTasks(t.Context(), l, host, container, 2, 0)
	if err != nil || len(claimed) != 2 {
		t.Fatalf("claim %v %v", claimed, err)
	}
	source := LogSource{Kind: LogsOfContainer, ID: uuid.UUID(container)}
	read := func(after int64) []LogEntry {
		var out []LogEntry
		err := e.StreamLogs(t.Context(), l, f.workspace, source, LogQuery{After: after}, func(batch []LogEntry) error {
			out = append(out, batch...)
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
		return out
	}
	line := func(n int, data string) []LogLine {
		return []LogLine{{Attempt: claimed[n].Attempt, Stream: LogStdout, Data: data, Time: time.Now()}}
	}

	// The first writer takes the lower id and commits last.
	slow, err := pool.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = slow.Rollback(t.Context()) }()
	if _, err := New(slow).InsertLogs(t.Context(), InsertLogsParams{
		AttemptIds: []uuid.UUID{uuid.UUID(claimed[0].Attempt)}, Streams: []string{"stdout"}, Data: []string{"first"},
		LoggedAt: []time.Time{time.Now()}, ContainerID: uuid.UUID(container), HostID: hostUUID(host),
	}); err != nil {
		t.Fatal(err)
	}
	if err := e.AppendLogs(t.Context(), host, container, line(1, "second")); err != nil {
		t.Fatal(err)
	}
	if got := read(0); len(got) != 0 {
		t.Fatalf("read while an older writer runs returned %+v", got)
	}
	if err := slow.Commit(t.Context()); err != nil {
		t.Fatal(err)
	}
	first := read(0)
	if len(first) != 2 || first[0].Data != "first" || first[1].Data != "second" {
		t.Fatalf("lines after both commit %+v, want first then second", first)
	}
	if err := e.AppendLogs(t.Context(), host, container, line(0, "third")); err != nil {
		t.Fatal(err)
	}
	// Resuming after the last entry, whose id is below the first's, yields
	// only the newer line.
	if got := read(first[1].ID); len(got) != 1 || got[0].Data != "third" {
		t.Fatalf("resumed lines %+v, want third", got)
	}
}

func TestLogsByWorkloadAndContainerWithTail(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	host, container := placedContainer(t, pool, f, ContainerReady, 2)
	submit(t, e, f, 2)
	claimed, err := e.ClaimTasks(t.Context(), l, host, container, 2, 0)
	if err != nil || len(claimed) != 2 {
		t.Fatalf("claim %v %v", claimed, err)
	}
	var lines []LogLine
	for n := range 5 {
		lines = append(lines, LogLine{Attempt: claimed[n%2].Attempt, Stream: LogStdout, Data: string(rune('a' + n)), Time: time.Now()})
	}
	if err := e.AppendLogs(t.Context(), host, container, lines); err != nil {
		t.Fatal(err)
	}
	var workload uuid.UUID
	if err := pool.QueryRow(t.Context(), "select workload_id from releases where id = $1", f.release).Scan(&workload); err != nil {
		t.Fatal(err)
	}
	read := func(source LogSource, tail int) string {
		var out string
		err := e.StreamLogs(t.Context(), l, f.workspace, source, LogQuery{Tail: tail}, func(batch []LogEntry) error {
			for _, entry := range batch {
				out += entry.Data
			}
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
		return out
	}
	if got := read(LogSource{Kind: LogsOfWorkload, ID: workload}, 0); got != "abcde" {
		t.Fatalf("workload logs %q", got)
	}
	if got := read(LogSource{Kind: LogsOfContainer, ID: uuid.UUID(container)}, 2); got != "de" {
		t.Fatalf("container tail %q", got)
	}
	if got := read(LogSource{Kind: LogsOfTask, ID: uuid.UUID(claimed[0].Task)}, 0); got != "ace" {
		t.Fatalf("task logs %q", got)
	}
	err = e.CheckLogSource(t.Context(), f.workspace, LogSource{Kind: LogsOfContainer, ID: uuid.New()})
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown container logs %v", err)
	}
}
