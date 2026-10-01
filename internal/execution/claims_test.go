package execution

import (
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func TestConcurrentClaimsNeverShareATask(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 1000, "timeout_seconds": 60}`)
	submit(t, e, f, 200)

	const containers = 8
	var wg sync.WaitGroup
	var mu sync.Mutex
	seen := map[TaskID]int{}
	for range containers {
		host, container := placedContainer(t, pool, f, ContainerReady, 4)
		wg.Go(func() {
			for {
				claimed, err := e.ClaimTasks(t.Context(), l, host, container, 4, 0)
				if err != nil {
					t.Errorf("claim: %v", err)
					return
				}
				if len(claimed) == 0 {
					return
				}
				for _, c := range claimed {
					if c.Deadline.Before(time.Now().Add(50 * time.Second)) {
						t.Errorf("deadline %v is not the release timeout from now", c.Deadline)
					}
					// Free the slot so the container keeps claiming.
					err := e.CompleteAttempt(t.Context(), host, container, AttemptOutcome{
						Attempt: c.Attempt, State: AttemptSucceeded, Result: &Payload{Encoding: EncodingJSON, Data: []byte("1")},
					})
					if err != nil {
						t.Errorf("complete: %v", err)
					}
				}
				mu.Lock()
				for _, c := range claimed {
					seen[c.Task]++
				}
				mu.Unlock()
			}
		})
	}
	wg.Wait()
	if len(seen) != 200 {
		t.Fatalf("claimed %d distinct tasks, want 200", len(seen))
	}
	for task, n := range seen {
		if n != 1 {
			t.Fatalf("task %s claimed %d times", task, n)
		}
	}
}

func TestClaimRequiresAReadyContainerOnTheHost(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	submit(t, e, f, 3)

	host, starting := placedContainer(t, pool, f, ContainerStarting, 1)
	if claimed, err := e.ClaimTasks(t.Context(), l, host, starting, 1, 0); err != nil || len(claimed) != 0 {
		t.Fatalf("starting container claimed %d tasks, err %v", len(claimed), err)
	}
	owner, ready := placedContainer(t, pool, f, ContainerReady, 1)
	if _, err := e.ClaimTasks(t.Context(), l, host, ready, 1, 0); !errors.Is(err, ErrNotAssigned) {
		t.Fatalf("another host's container: got %v, want ErrNotAssigned", err)
	}
	// Slots bound the claim even when the host asks for more.
	claimed, err := e.ClaimTasks(t.Context(), l, owner, ready, 5, 0)
	if err != nil || len(claimed) != 1 {
		t.Fatalf("ready container claimed %d tasks, err %v; want one per slot", len(claimed), err)
	}
	if again, err := e.ClaimTasks(t.Context(), l, owner, ready, 5, 0); err != nil || len(again) != 0 {
		t.Fatalf("full container claimed %d more, err %v", len(again), err)
	}
}

func TestClaimWaitsForSubmit(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	host, container := placedContainer(t, pool, f, ContainerReady, 1)

	done := make(chan []ClaimedTask, 1)
	start := time.Now()
	go func() {
		claimed, err := e.ClaimTasks(t.Context(), l, host, container, 1, 10*time.Second)
		if err != nil {
			t.Error(err)
		}
		done <- claimed
	}()
	time.Sleep(200 * time.Millisecond)
	submit(t, e, f, 1)
	claimed := <-done
	if len(claimed) != 1 || time.Since(start) > 5*time.Second {
		t.Fatalf("claimed %d after %v; want the submit to wake the claim", len(claimed), time.Since(start))
	}
	if string(claimed[0].Input.Data) != `{"args": [1], "kwargs": {}}` {
		t.Fatalf("input %q", claimed[0].Input.Data)
	}
}

func TestClaimWakesWhenContainerBecomesReady(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	submit(t, e, f, 1)
	host, container := placedContainer(t, pool, f, ContainerStarting, 1)

	done := make(chan []ClaimedTask, 1)
	start := time.Now()
	go func() {
		claimed, err := e.ClaimTasks(t.Context(), l, host, container, 1, 30*time.Second)
		if err != nil {
			t.Error(err)
		}
		done <- claimed
	}()
	time.Sleep(200 * time.Millisecond)
	if _, err := e.ApplyReport(t.Context(), host, ContainerReport{Container: container, Phase: ReportReady, ObservedAt: time.Now()}); err != nil {
		t.Fatal(err)
	}
	claimed := <-done
	if len(claimed) != 1 || time.Since(start) > 5*time.Second {
		t.Fatalf("claimed %d after %v; want the ready report to wake the claim", len(claimed), time.Since(start))
	}
}

func TestClaimCapsTotalInputBytes(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	input := Payload{Encoding: EncodingCloudpickle, Data: make([]byte, MaxPayloadBytes)}
	if _, err := e.Submit(t.Context(), SubmitRequest{
		Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: []Payload{input, input, input, input, input},
	}); err != nil {
		t.Fatal(err)
	}
	host, container := placedContainer(t, pool, f, ContainerReady, 5)

	claimed, err := e.ClaimTasks(t.Context(), l, host, container, 5, 0)
	if err != nil {
		t.Fatal(err)
	}
	total := 0
	for _, c := range claimed {
		total += len(c.Input.Data)
	}
	if want := MaxClaimInputBytes / MaxPayloadBytes; len(claimed) != want || total > MaxClaimInputBytes {
		t.Fatalf("claimed %d tasks with %d input bytes; want %d within %d", len(claimed), total, want, MaxClaimInputBytes)
	}
	// The rest stays queued for the next claim.
	claimed, err = e.ClaimTasks(t.Context(), l, host, container, 5, 0)
	if err != nil || len(claimed) != 1 {
		t.Fatalf("second claim got %d: %v", len(claimed), err)
	}
}

func TestCompletionIsFencedByContainerAndHost(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	submit(t, e, f, 1)
	host, container := placedContainer(t, pool, f, ContainerReady, 1)
	_, otherContainer := placedContainer(t, pool, f, ContainerReady, 1)
	claimed, err := e.ClaimTasks(t.Context(), l, host, container, 1, 0)
	if err != nil || len(claimed) != 1 {
		t.Fatalf("claim: %d, %v", len(claimed), err)
	}
	result := &Payload{Encoding: EncodingJSON, Data: []byte(`5500`)}
	success := AttemptOutcome{Attempt: claimed[0].Attempt, State: AttemptSucceeded, Result: result}

	if err := e.CompleteAttempt(t.Context(), host, otherContainer, success); !errors.Is(err, ErrStaleAttempt) {
		t.Fatalf("wrong container: got %v, want ErrStaleAttempt", err)
	}
	if err := e.CompleteAttempt(t.Context(), compute.HostID(uuid.New()), container, success); !errors.Is(err, ErrStaleAttempt) {
		t.Fatalf("wrong host: got %v, want ErrStaleAttempt", err)
	}
	if err := e.CompleteAttempt(t.Context(), host, container, success); err != nil {
		t.Fatal(err)
	}
	if err := e.CompleteAttempt(t.Context(), host, container, success); !errors.Is(err, ErrStaleAttempt) {
		t.Fatalf("duplicate: got %v, want ErrStaleAttempt", err)
	}
	got, err := e.TaskResult(t.Context(), f.workspace, claimed[0].Task)
	if err != nil || string(got.Data) != "5500" {
		t.Fatalf("result %q, err %v", got.Data, err)
	}
}

func TestCrashedSlotRetriesAsUserError(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10, "retry_policy": {"max_attempts": 2}}`)
	tasks := submit(t, e, f, 1)
	host, container := placedContainer(t, pool, f, ContainerReady, 1)
	for attempt := 1; attempt <= 2; attempt++ {
		claimed, err := e.ClaimTasks(t.Context(), l, host, container, 1, 5*time.Second)
		if err != nil || len(claimed) != 1 || claimed[0].Number != attempt {
			t.Fatalf("attempt %d: claimed %v, err %v", attempt, claimed, err)
		}
		crash := CrashFailure("")
		if err := e.CompleteAttempt(t.Context(), host, container, AttemptOutcome{Attempt: claimed[0].Attempt, State: AttemptFailed, Failure: &crash}); err != nil {
			t.Fatal(err)
		}
	}
	task, err := e.GetTask(t.Context(), l, f.workspace, tasks[0].ID, 0)
	if err != nil {
		t.Fatal(err)
	}
	if task.Status != TaskFailed || task.Attempts != 2 || task.Failure == nil || task.Failure.Type != "WorkerCrashed" {
		t.Fatalf("task %+v; want failed after two crashed attempts", task)
	}
}
