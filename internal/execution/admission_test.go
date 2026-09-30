package execution

import (
	"errors"
	"sync"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func TestSubmitEnforcesMaxPendingUnderConcurrency(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)

	const submitters = 8
	var wg sync.WaitGroup
	accepted := make(chan int, submitters)
	for range submitters {
		wg.Go(func() {
			tasks, err := e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: jsonInputs(3)})
			var tooMany *TooManyPendingError
			switch {
			case err == nil:
				accepted <- len(tasks)
			case errors.As(err, &tooMany):
			default:
				t.Errorf("submit: %v", err)
			}
		})
	}
	wg.Wait()
	close(accepted)
	total := 0
	for n := range accepted {
		total += n
	}
	var queued int
	if err := pool.QueryRow(t.Context(), "select count(*) from tasks where status = 'queued'").Scan(&queued); err != nil {
		t.Fatal(err)
	}
	// Three batches of three fit under ten; a fourth would exceed it.
	if total != 9 || queued != 9 {
		t.Fatalf("accepted %d tasks, %d queued; want 9", total, queued)
	}
}

func TestSubmitBatchIsOneTransactionInInputOrder(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 1000, "retry_policy": {"max_attempts": 3}}`)

	inputs := make([]Payload, 1000)
	for i := range inputs {
		inputs[i] = Payload{Encoding: EncodingJSON, Data: []byte(`{"args": [` + string(rune('0'+i%10)) + `], "kwargs": {}}`)}
	}
	tasks, err := e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: inputs})
	if err != nil {
		t.Fatal(err)
	}
	if len(tasks) != 1000 {
		t.Fatalf("got %d tasks", len(tasks))
	}
	var xacts, distinctCreated, maxAttempts, mismatched int
	if err := pool.QueryRow(t.Context(), `
select count(distinct xmin::text), count(distinct created_at), max(max_attempts) from tasks`).Scan(&xacts, &distinctCreated, &maxAttempts); err != nil {
		t.Fatal(err)
	}
	if xacts != 1 || distinctCreated != 1 || maxAttempts != 3 {
		t.Fatalf("tasks from %d transactions, %d timestamps, max_attempts %d; want one transaction and 3", xacts, distinctCreated, maxAttempts)
	}
	for i, task := range tasks {
		var data []byte
		if err := pool.QueryRow(t.Context(), "select data from task_inputs where task_id = $1", task.ID.String()).Scan(&data); err != nil {
			t.Fatal(err)
		}
		if string(data) != string(inputs[i].Data) {
			mismatched++
		}
	}
	if mismatched != 0 {
		t.Fatalf("%d tasks returned out of input order", mismatched)
	}

	// Anything beyond the limit rejects the whole batch.
	_, err = e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: jsonInputs(1)})
	var tooMany *TooManyPendingError
	if !errors.As(err, &tooMany) {
		t.Fatalf("got %v, want TooManyPendingError", err)
	}
}

func TestSubmitToStoppedFunctionIsRejected(t *testing.T) {
	pool := dbtest.New(t)
	e := NewExecution(pool)
	f := deployedFunction(t, pool, `{"max_pending_tasks": 10}`)
	if _, err := pool.Exec(t.Context(), "update workloads set desired_state = 'stopped'"); err != nil {
		t.Fatal(err)
	}
	_, err := e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: jsonInputs(1)})
	if !errors.Is(err, ErrNotAccepting) {
		t.Fatalf("got %v, want ErrNotAccepting", err)
	}
	_, err = e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "missing", Inputs: jsonInputs(1)})
	if !errors.Is(err, ErrNotFound) {
		t.Fatalf("got %v, want ErrNotFound", err)
	}
}
