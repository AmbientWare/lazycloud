package execution

import (
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func containerState(t *testing.T, e *Execution, id ContainerID) ContainerState {
	t.Helper()
	var s string
	if err := e.pool.QueryRow(t.Context(), "select state from containers where id = $1", uuid.UUID(id)).Scan(&s); err != nil {
		t.Fatal(err)
	}
	return ContainerState(s)
}

func TestReportsDriveContainerLifecycle(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	host, container := placedContainer(t, pool, f, ContainerStarting, 1)
	other, _ := placedContainer(t, pool, f, ContainerStarting, 1)

	// Another host's report neither readies the container nor keeps it.
	actions, err := e.ApplyReport(t.Context(), other, ContainerReport{Container: container, Phase: ReportReady})
	if err != nil || len(actions.Stop) != 1 || containerState(t, e, container) != ContainerStarting {
		t.Fatalf("foreign ready report: %+v, %v", actions, err)
	}
	if _, err := pool.Exec(t.Context(), "update releases set start_failures = 2"); err != nil {
		t.Fatal(err)
	}
	if _, err := e.ApplyReport(t.Context(), host, ContainerReport{Container: container, Phase: ReportReady}); err != nil {
		t.Fatal(err)
	}
	var failures int
	if err := pool.QueryRow(t.Context(), "select start_failures from releases").Scan(&failures); err != nil {
		t.Fatal(err)
	}
	if containerState(t, e, container) != ContainerReady || failures != 0 {
		t.Fatalf("ready report: state %s, start failures %d", containerState(t, e, container), failures)
	}

	tasks := submit(t, e, f, 2)
	loadError := &Failure{Kind: FailureLoadError, Type: "ImportError", Message: "no module named reports"}
	if _, err := e.ApplyReport(t.Context(), host, ContainerReport{
		Container: container, Phase: ReportExited,
		Exit: &ContainerExit{Reason: StopLoadError, Message: "import failed", LoadError: loadError},
	}); err != nil {
		t.Fatal(err)
	}
	if containerState(t, e, container) != ContainerStopped || status(t, pool, tasks[0].ID) != TaskFailed {
		t.Fatal("a load error must stop the container and fail queued tasks")
	}
	// A stopped container the host still reports must stop.
	actions, err = e.ApplyReport(t.Context(), host, ContainerReport{Container: container, Phase: ReportReady})
	if err != nil || len(actions.Stop) != 1 || actions.Stop[0] != container {
		t.Fatalf("report of stopped container: %+v, %v", actions, err)
	}
}

func TestReconcileStopsMissingAndUnknownContainers(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10, "retry_policy": {"max_attempts": 2}}`)
	tasks := submit(t, e, f, 1)
	host, missing := placedContainer(t, pool, f, ContainerReady, 1)
	claimed, err := e.ClaimTasks(t.Context(), l, host, missing, 1, 0)
	if err != nil || len(claimed) != 1 {
		t.Fatal(err)
	}
	var starting ContainerID
	var startingID uuid.UUID
	if err := pool.QueryRow(t.Context(), `
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes)
values ($1, $2, 'starting', $3, 1, 1000, 1 << 28) returning id`, uuid.UUID(f.workspace), f.release, uuid.UUID(host)).Scan(&startingID); err != nil {
		t.Fatal(err)
	}
	starting = ContainerID(startingID)
	unknown := ContainerID(uuid.New())
	staleAttempt := AttemptID(uuid.New())

	actions, err := e.ReconcileHost(t.Context(), host, []ContainerReport{
		{Container: unknown, Phase: ReportReady, Running: []AttemptID{staleAttempt}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(actions.Stop) != 1 || actions.Stop[0] != unknown {
		t.Fatalf("stop %v, want the unknown container", actions.Stop)
	}
	if containerState(t, e, missing) != ContainerStopped {
		t.Fatal("a ready container the host no longer runs must stop")
	}
	// Its attempt was lost and the task waits for a retry.
	if status(t, pool, tasks[0].ID) != TaskQueued {
		t.Fatalf("task %s, want queued for retry", status(t, pool, tasks[0].ID))
	}
	commands, err := e.HostCommands(t.Context(), host)
	if err != nil {
		t.Fatal(err)
	}
	if len(commands.Start) != 1 || commands.Start[0].Container != starting {
		t.Fatalf("start commands %+v, want the unreported starting container", commands.Start)
	}
}

func TestReportedAttemptThatEndedIsCancelled(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	tasks := submit(t, e, f, 1)
	host, container := placedContainer(t, pool, f, ContainerReady, 1)
	claimed, err := e.ClaimTasks(t.Context(), l, host, container, 1, 0)
	if err != nil || len(claimed) != 1 {
		t.Fatal(err)
	}
	if _, err := e.CancelTask(t.Context(), f.workspace, tasks[0].ID); err != nil {
		t.Fatal(err)
	}
	actions, err := e.ApplyReport(t.Context(), host, ContainerReport{
		Container: container, Phase: ReportReady, Running: []AttemptID{claimed[0].Attempt},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(actions.Cancel) != 1 || actions.Cancel[0].Attempt != claimed[0].Attempt {
		t.Fatalf("cancel %+v, want the cancelled attempt", actions.Cancel)
	}
}

// After an agent restart, Hello lists adopted containers as starting until
// their slots report ready again, and recently exited ones as exited.
func TestHelloAdoptsStartingAndAppliesExited(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	host, adopted := placedContainer(t, pool, f, ContainerReady, 1)
	var exitedID uuid.UUID
	if err := pool.QueryRow(t.Context(), `
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes)
values ($1, $2, 'ready', $3, 1, 1000, 1 << 28) returning id`, uuid.UUID(f.workspace), f.release, uuid.UUID(host)).Scan(&exitedID); err != nil {
		t.Fatal(err)
	}
	exited := ContainerID(exitedID)
	for range 2 { // A repeated Hello changes nothing further.
		actions, err := e.ReconcileHost(t.Context(), host, []ContainerReport{
			{Container: adopted, Phase: ReportStarting},
			{Container: exited, Phase: ReportExited, Exit: &ContainerExit{Reason: StopOutOfMemory}},
		})
		if err != nil || len(actions.Stop) != 0 {
			t.Fatalf("reconcile: %+v, %v", actions, err)
		}
	}
	if containerState(t, e, adopted) != ContainerReady || containerState(t, e, exited) != ContainerStopped {
		t.Fatalf("adopted %s, exited %s; want ready and stopped", containerState(t, e, adopted), containerState(t, e, exited))
	}
}
