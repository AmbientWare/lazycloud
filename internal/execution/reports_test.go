package execution

import (
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func stateOf(t *testing.T, e *Execution, id ContainerID) ContainerState {
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
	if err != nil || len(actions.Stop) != 1 || stateOf(t, e, container) != ContainerStarting {
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
	if stateOf(t, e, container) != ContainerReady || failures != 0 {
		t.Fatalf("ready report: state %s, start failures %d", stateOf(t, e, container), failures)
	}

	tasks := submit(t, e, f, 2)
	loadError := &Failure{Kind: FailureLoadError, Type: "ImportError", Message: "no module named reports"}
	if _, err := e.ApplyReport(t.Context(), host, ContainerReport{
		Container: container, Phase: ReportExited,
		Exit: &ContainerExit{Reason: StopLoadError, Message: "import failed", LoadError: loadError},
	}); err != nil {
		t.Fatal(err)
	}
	if stateOf(t, e, container) != ContainerStopped || status(t, pool, tasks[0].ID) != TaskFailed {
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
	if stateOf(t, e, missing) != ContainerStopped {
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
	if stateOf(t, e, adopted) != ContainerReady || stateOf(t, e, exited) != ContainerStopped {
		t.Fatalf("adopted %s, exited %s; want ready and stopped", stateOf(t, e, adopted), stateOf(t, e, exited))
	}
}

// A ready report lists every attempt the container's slots run. Running
// attempts it omits that started before it are lost and retried; an adopted
// container's starting report and attempts claimed after the report keep
// running.
func TestReadyReportLosesOmittedAttempts(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10, "retry_policy": {"max_attempts": 2}}`)
	submit(t, e, f, 2)
	host, container := placedContainer(t, pool, f, ContainerReady, 2)
	beforeClaim := time.Now().Add(-time.Second)
	claimed, err := e.ClaimTasks(t.Context(), l, host, container, 2, 0)
	if err != nil || len(claimed) != 2 {
		t.Fatalf("claimed %d: %v", len(claimed), err)
	}
	kept, omitted := claimed[0], claimed[1]

	for _, report := range []ContainerReport{
		{Container: container, Phase: ReportStarting, ObservedAt: time.Now()},
		{Container: container, Phase: ReportReady, ObservedAt: beforeClaim},
	} {
		if _, err := e.ReconcileHost(t.Context(), host, []ContainerReport{report}); err != nil {
			t.Fatal(err)
		}
		if status(t, pool, kept.Task) != TaskRunning || status(t, pool, omitted.Task) != TaskRunning {
			t.Fatalf("after a %s report observed at %v the tasks must keep running", report.Phase, report.ObservedAt)
		}
	}

	// A claim response may still be in flight right after the claim commits.
	if _, err := e.ApplyReport(t.Context(), host, ContainerReport{
		Container: container, Phase: ReportReady, Running: []AttemptID{kept.Attempt}, ObservedAt: time.Now(),
	}); err != nil {
		t.Fatal(err)
	}
	if status(t, pool, omitted.Task) != TaskRunning {
		t.Fatal("an attempt claimed moments before the report must keep running")
	}
	if _, err := e.ApplyReport(t.Context(), host, ContainerReport{
		Container: container, Phase: ReportReady, Running: []AttemptID{kept.Attempt}, ObservedAt: time.Now().Add(2 * time.Minute),
	}); err != nil {
		t.Fatal(err)
	}
	var attemptState string
	if err := pool.QueryRow(t.Context(), "select state from attempts where id = $1", uuid.UUID(omitted.Attempt)).Scan(&attemptState); err != nil {
		t.Fatal(err)
	}
	if attemptState != string(AttemptLost) || status(t, pool, omitted.Task) != TaskQueued {
		t.Fatalf("omitted attempt %s, task %s; want lost and queued for a retry", attemptState, status(t, pool, omitted.Task))
	}
	if status(t, pool, kept.Task) != TaskRunning {
		t.Fatal("the reported attempt must keep running")
	}
}
