package execution

import (
	"encoding/json"
	"errors"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func dependentInput(upstream ...TaskID) TaskInput {
	return TaskInput{
		Payload:   Payload{Encoding: EncodingCloudpickle, Data: []byte("pickle")},
		DependsOn: upstream,
	}
}

func submitInputs(t *testing.T, e *Execution, f functionFixture, inputs ...TaskInput) []Task {
	t.Helper()
	tasks, err := e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: inputs})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	return tasks
}

func failureKind(t *testing.T, e *Execution, f functionFixture, id TaskID) FailureKind {
	t.Helper()
	task, err := e.readTask(t.Context(), f.workspace, id)
	if err != nil {
		t.Fatal(err)
	}
	if task.Failure == nil {
		return ""
	}
	return task.Failure.Kind
}

func TestDependentWaitsForUpstreamThenReceivesItsResult(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	host, container := placedContainer(t, pool, f, ContainerReady, 2)

	upstream := submit(t, e, f, 1)[0]
	dependent := submitInputs(t, e, f, dependentInput(upstream.ID))[0]
	view, err := e.readTask(t.Context(), f.workspace, dependent.ID)
	if err != nil {
		t.Fatal(err)
	}
	if view.Pending == nil || view.Pending.Reason != PendingDependencies {
		t.Fatalf("dependent pending %+v, want dependencies", view.Pending)
	}

	// Only the upstream is claimable, though the container has two slots.
	claimed, err := e.ClaimTasks(t.Context(), l, host, container, 2, 0)
	if err != nil || len(claimed) != 1 || claimed[0].Task != upstream.ID || claimed[0].Root != upstream.ID {
		t.Fatalf("first claim %+v %v, want only the upstream rooted at itself", claimed, err)
	}
	result := Payload{Encoding: EncodingCloudpickle, Data: []byte("upstream result")}
	if err := e.CompleteAttempt(t.Context(), host, container, AttemptOutcome{
		Attempt: claimed[0].Attempt, State: AttemptSucceeded, Result: &result,
	}); err != nil {
		t.Fatal(err)
	}

	claimed, err = e.ClaimTasks(t.Context(), l, host, container, 2, 0)
	if err != nil || len(claimed) != 1 || claimed[0].Task != dependent.ID {
		t.Fatalf("second claim %+v %v, want the dependent", claimed, err)
	}
	deps := claimed[0].Dependencies
	if len(deps) != 1 || deps[0].Task != upstream.ID || string(deps[0].Result.Data) != "upstream result" {
		t.Fatalf("dependency results %+v", deps)
	}
}

func TestUnsuccessfulUpstreamFailsDependentsTransitively(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)

	a := submit(t, e, f, 1)[0]
	b := submitInputs(t, e, f, dependentInput(a.ID))[0]
	c := submitInputs(t, e, f, dependentInput(b.ID))[0]
	unrelated := submit(t, e, f, 1)[0]

	if _, err := e.CancelTask(t.Context(), f.workspace, a.ID); err != nil {
		t.Fatal(err)
	}
	for _, id := range []TaskID{b.ID, c.ID} {
		if s, kind := status(t, pool, id), failureKind(t, e, f, id); s != TaskFailed || kind != FailureDependencyFailed {
			t.Fatalf("dependent %s is %s (%s), want failed dependency_failed", id, s, kind)
		}
	}
	if s := status(t, pool, unrelated.ID); s != TaskQueued {
		t.Fatalf("unrelated task is %s", s)
	}

	// A submit that names a failed upstream fails at once.
	late := submitInputs(t, e, f, dependentInput(a.ID))[0]
	if late.Status != TaskFailed || status(t, pool, late.ID) != TaskFailed {
		t.Fatalf("late dependent is %s, want failed", late.Status)
	}
}

func TestSubmitRejectsUpstreamOutsideTheWorkspace(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	missing := TaskID(uuid.New())
	_, err := e.Submit(t.Context(), SubmitRequest{
		Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: []TaskInput{dependentInput(missing)},
	})
	var unknown *UnknownTaskError
	if !errors.As(err, &unknown) || unknown.Task != missing {
		t.Fatalf("submit error %v, want unknown upstream %s", err, missing)
	}
	var tasks int
	if err := pool.QueryRow(t.Context(), "select count(*) from tasks").Scan(&tasks); err != nil || tasks != 0 {
		t.Fatalf("%d tasks after a rejected submit (%v)", tasks, err)
	}
}

func TestOversizedDependencyResultsFailTheDependent(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	host, container := placedContainer(t, pool, f, ContainerReady, 4)

	ups := submit(t, e, f, 4)
	dependent := submitInputs(t, e, f, dependentInput(ups[0].ID, ups[1].ID, ups[2].ID, ups[3].ID))[0]
	claimed, err := e.ClaimTasks(t.Context(), l, host, container, 4, 0)
	if err != nil || len(claimed) != 4 {
		t.Fatalf("claim %d %v", len(claimed), err)
	}
	// Four 16 MiB results exceed the 48 MiB a dependent may receive.
	result := Payload{Encoding: EncodingCloudpickle, Data: make([]byte, MaxPayloadBytes)}
	for _, c := range claimed {
		if err := e.CompleteAttempt(t.Context(), host, container, AttemptOutcome{
			Attempt: c.Attempt, State: AttemptSucceeded, Result: &result,
		}); err != nil {
			t.Fatal(err)
		}
	}
	assertTooLarge := func(id TaskID) {
		t.Helper()
		task, err := e.readTask(t.Context(), f.workspace, id)
		if err != nil {
			t.Fatal(err)
		}
		if task.Status != TaskFailed || task.Failure == nil || task.Failure.Type != "DependenciesTooLarge" {
			encoded, _ := json.Marshal(task.Failure)
			t.Fatalf("dependent %s %s, want failed DependenciesTooLarge", task.Status, encoded)
		}
	}
	assertTooLarge(dependent.ID)

	// The cap holds when every upstream had succeeded before the submit,
	// and for a rerun, which no later resolution checks.
	late := submitInputs(t, e, f, dependentInput(ups[0].ID, ups[1].ID, ups[2].ID, ups[3].ID))[0]
	if late.Status != TaskFailed {
		t.Fatalf("late dependent submitted as %s", late.Status)
	}
	assertTooLarge(late.ID)
	rerun, err := e.RerunTask(t.Context(), f.workspace, late.ID)
	if err != nil {
		t.Fatal(err)
	}
	assertTooLarge(rerun.ID)
	if fits := submitInputs(t, e, f, dependentInput(ups[0].ID, ups[1].ID))[0]; fits.Status != TaskQueued {
		t.Fatalf("dependent within the cap is %s", fits.Status)
	}
}
