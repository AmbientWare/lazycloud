package execution

import (
	"errors"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func TestCancelQueuedAndRunningTasks(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	l := listen(t, pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	tasks := submit(t, e, f, 2)

	queued, err := e.CancelTask(t.Context(), f.workspace, tasks[1].ID)
	if err != nil || queued.Status != TaskCancelled || queued.FinishedAt == nil {
		t.Fatalf("cancel queued: %+v, %v", queued, err)
	}

	host, container := placedContainer(t, pool, f, ContainerReady, 1)
	claimed, err := e.ClaimTasks(t.Context(), l, host, container, 1, 0)
	if err != nil || len(claimed) != 1 || claimed[0].Task != tasks[0].ID {
		t.Fatalf("claim: %v, %v", claimed, err)
	}
	running, err := e.CancelTask(t.Context(), f.workspace, tasks[0].ID)
	if err != nil || running.Status != TaskCancelled {
		t.Fatalf("cancel running: %+v, %v", running, err)
	}
	var attempt string
	if err := pool.QueryRow(t.Context(), "select state from attempts where id = $1", claimed[0].Attempt.String()).Scan(&attempt); err != nil {
		t.Fatal(err)
	}
	if AttemptState(attempt) != AttemptCancelled {
		t.Fatalf("attempt %s, want cancelled", attempt)
	}
	// The host is told to kill the slot, and its late completion is stale.
	commands, err := e.HostCommands(t.Context(), host)
	if err != nil {
		t.Fatal(err)
	}
	if len(commands.Cancel) != 1 || commands.Cancel[0].Attempt != claimed[0].Attempt || commands.Cancel[0].Reason != AttemptCancelled {
		t.Fatalf("cancel commands %+v", commands.Cancel)
	}
	late := AttemptOutcome{Attempt: claimed[0].Attempt, State: AttemptSucceeded, Result: &Payload{Encoding: EncodingJSON, Data: []byte("1")}}
	if err := e.CompleteAttempt(t.Context(), host, container, late); !errors.Is(err, ErrStaleAttempt) {
		t.Fatalf("late completion: got %v, want ErrStaleAttempt", err)
	}
	if status(t, pool, tasks[0].ID) != TaskCancelled {
		t.Fatal("late completion changed a cancelled task")
	}
	if _, err := e.TaskResult(t.Context(), f.workspace, tasks[0].ID); !errors.Is(err, ErrNoResult) {
		t.Fatalf("result of cancelled task: got %v, want ErrNoResult", err)
	}
}

func TestGetTaskWaitWakesOnCompletion(t *testing.T) {
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

	if _, err := e.TaskResult(t.Context(), f.workspace, tasks[0].ID); !errors.Is(err, ErrTaskNotFinished) {
		t.Fatalf("result while running: got %v, want ErrTaskNotFinished", err)
	}
	go func() {
		time.Sleep(200 * time.Millisecond)
		_ = e.CompleteAttempt(t.Context(), host, container, AttemptOutcome{
			Attempt: claimed[0].Attempt, State: AttemptSucceeded, Result: &Payload{Encoding: EncodingJSON, Data: []byte("1")},
		})
	}()
	start := time.Now()
	task, err := e.GetTask(t.Context(), l, f.workspace, tasks[0].ID, 30*time.Second)
	if err != nil || task.Status != TaskSucceeded || time.Since(start) > 5*time.Second {
		t.Fatalf("wait returned %s after %v, err %v", task.Status, time.Since(start), err)
	}
}

func TestFollowLogsEndsWhenTaskFinishes(t *testing.T) {
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
	attempt := claimed[0].Attempt
	line := func(data string) LogLine {
		return LogLine{Attempt: attempt, Stream: LogStdout, Data: data, Time: time.Now()}
	}
	if err := e.AppendLogs(t.Context(), host, container, []LogLine{line("one\n"), line("two\x00\n")}); err != nil {
		t.Fatal(err)
	}

	got := make(chan []LogEntry, 16)
	done := make(chan error, 1)
	go func() {
		done <- e.StreamLogs(t.Context(), l, f.workspace, tasks[0].ID, 0, true, func(batch []LogEntry) error {
			got <- batch
			return nil
		})
	}()
	first := <-got
	if len(first) != 2 || first[0].Data != "one\n" || first[1].Data != "two�\n" {
		t.Fatalf("first batch %+v", first)
	}
	if err := e.AppendLogs(t.Context(), host, container, []LogLine{line("three\n")}); err != nil {
		t.Fatal(err)
	}
	if second := <-got; len(second) != 1 || second[0].Data != "three\n" || second[0].ID <= first[1].ID {
		t.Fatalf("second batch %+v", second)
	}
	select {
	case err := <-done:
		t.Fatalf("stream ended while the task runs: %v", err)
	case <-time.After(200 * time.Millisecond):
	}
	if err := e.CompleteAttempt(t.Context(), host, container, AttemptOutcome{
		Attempt: attempt, State: AttemptSucceeded, Result: &Payload{Encoding: EncodingJSON, Data: []byte("1")},
	}); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("follow did not end after the task finished")
	}

	// Another host cannot write into the attempt's log.
	other, _ := placedContainer(t, pool, f, ContainerReady, 1)
	if err := e.AppendLogs(t.Context(), other, container, []LogLine{line("forged\n")}); err != nil {
		t.Fatal(err)
	}
	var lines int
	if err := pool.QueryRow(t.Context(), "select count(*) from task_logs").Scan(&lines); err != nil {
		t.Fatal(err)
	}
	if lines != 3 {
		t.Fatalf("%d stored lines, want 3", lines)
	}
}
