package observability_test

import (
	"errors"
	"log/slog"
	"strings"
	"testing"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

func (f *fixture) claim(host compute.HostID, container execution.ContainerID) execution.ClaimedTask {
	f.t.Helper()
	listener := database.NewListener(f.pool, slog.New(slog.DiscardHandler), database.ChannelClaim)
	claimed, err := f.exec.ClaimTasks(f.t.Context(), listener, host, container, 1, 0)
	if err != nil || len(claimed) != 1 {
		f.t.Fatalf("claim: %v %+v", err, claimed)
	}
	return claimed[0]
}

func kinds(events []apitypes.TaskEvent) string {
	out := make([]string, len(events))
	for i, e := range events {
		out[i] = string(e.Kind)
	}
	return strings.Join(out, ",")
}

// The timeline is built from the task's and its attempts' rows: a failed
// attempt, the retry it scheduled, the attempt that succeeded and the
// outcome.
func TestTaskTimelineFollowsAttemptsAndRetries(t *testing.T) {
	f := newFixture(t, `{"max_pending_tasks": 100, "retry_policy": {"max_attempts": 2, "delay_seconds": 0}}`)
	host, container := f.placedContainer(f.release)
	task := f.submit(1)[0]

	first := f.claim(host, container)
	if err := f.exec.CompleteAttempt(t.Context(), host, container, execution.AttemptOutcome{
		Attempt: first.Attempt, State: execution.AttemptFailed,
		Failure: &execution.Failure{Kind: execution.FailureUserError, Message: "boom"},
	}); err != nil {
		t.Fatal(err)
	}
	waiting, err := f.obs.TaskTimeline(t.Context(), f.workspace, task.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got := kinds(waiting.Events); got != "submitted,attempt_started,attempt_finished,retry_scheduled" {
		t.Fatalf("timeline while waiting to retry: %s", got)
	}
	retry := waiting.Events[3]
	if retry.DueAt == nil || *retry.Attempt != 2 || *waiting.Events[2].Outcome != apitypes.AttemptOutcomeFailed ||
		*waiting.Events[1].ContainerId != uuid.UUID(container) {
		t.Fatalf("retry events %+v", waiting.Events)
	}

	second := f.claim(host, container)
	if err := f.exec.CompleteAttempt(t.Context(), host, container, execution.AttemptOutcome{
		Attempt: second.Attempt, State: execution.AttemptSucceeded,
		Result: &execution.Payload{Encoding: execution.EncodingJSON, Data: []byte(`1`)},
	}); err != nil {
		t.Fatal(err)
	}
	done, err := f.obs.TaskTimeline(t.Context(), f.workspace, task.ID)
	if err != nil {
		t.Fatal(err)
	}
	if got := kinds(done.Events); got != "submitted,attempt_started,attempt_finished,retry_scheduled,attempt_started,attempt_finished,finished" {
		t.Fatalf("finished timeline: %s", got)
	}
	if done.Status != apitypes.TaskStatusSucceeded || done.Events[3].DueAt != nil || *done.Events[6].Status != apitypes.TaskStatusSucceeded {
		t.Fatalf("finished timeline %+v", done)
	}
	if _, err := f.obs.TaskTimeline(t.Context(), f.workspace, execution.TaskID(uuid.New())); !errors.Is(err, observability.ErrNotFound) {
		t.Fatalf("unknown task: %v", err)
	}
}

// Any task of a call graph returns the whole graph from its root, parents
// before children, with dependencies; other workspaces cannot read it.
func TestCallGraphReadsTheWholeGraphFromAnyTask(t *testing.T) {
	f := newFixture(t, `{"max_pending_tasks": 100}`)
	otherWS, _, _, _ := f.addFunction("other", "reports", "summarize", `{}`)
	root := f.submit(1)[0]
	input := execution.TaskInput{Payload: execution.Payload{Encoding: execution.EncodingJSON, Data: []byte(`{"args": [], "kwargs": {}}`)}}
	children, err := f.exec.Submit(t.Context(), execution.SubmitRequest{
		Workspace: f.workspace, App: "reports", Function: "summarize", Parent: &root.ID,
		Inputs: []execution.TaskInput{input, input},
	})
	if err != nil {
		t.Fatal(err)
	}
	dependent := input
	dependent.DependsOn = []execution.TaskID{children[0].ID}
	grandchild, err := f.exec.Submit(t.Context(), execution.SubmitRequest{
		Workspace: f.workspace, App: "reports", Function: "summarize", Parent: &children[1].ID,
		Inputs: []execution.TaskInput{dependent},
	})
	if err != nil {
		t.Fatal(err)
	}
	f.submit(1) // An unrelated task stays out of the graph.

	graph, err := f.obs.TaskCallGraph(t.Context(), f.workspace, grandchild[0].ID)
	if err != nil {
		t.Fatal(err)
	}
	if graph.RootTaskId != uuid.UUID(root.ID) || graph.Truncated || len(graph.Nodes) != 4 {
		t.Fatalf("graph %+v", graph)
	}
	seen := map[uuid.UUID]bool{}
	for _, n := range graph.Nodes {
		if n.ParentTaskId != nil && !seen[*n.ParentTaskId] {
			t.Fatalf("node %s precedes its parent", n.TaskId)
		}
		seen[n.TaskId] = true
	}
	last := graph.Nodes[3]
	if last.TaskId != uuid.UUID(grandchild[0].ID) || len(last.DependsOn) != 1 || last.DependsOn[0] != uuid.UUID(children[0].ID) {
		t.Fatalf("grandchild node %+v", last)
	}
	if _, err := f.obs.TaskCallGraph(t.Context(), otherWS, root.ID); !errors.Is(err, observability.ErrNotFound) {
		t.Fatalf("another workspace read the graph: %v", err)
	}
}

// A submit made inside a sampled trace stores it, and the claim returns it
// so the host's attempt span joins that trace; an untraced submit stores
// none.
func TestClaimCarriesTheSubmittingTrace(t *testing.T) {
	f := newFixture(t, `{"max_pending_tasks": 100}`)
	host, container := f.placedContainer(f.release)
	sc := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: trace.TraceID{1, 2, 3}, SpanID: trace.SpanID{4, 5, 6}, TraceFlags: trace.FlagsSampled,
	})
	ctx := trace.ContextWithSpanContext(t.Context(), sc)
	input := execution.TaskInput{Payload: execution.Payload{Encoding: execution.EncodingJSON, Data: []byte(`{"args": [], "kwargs": {}}`)}}
	if _, err := f.exec.Submit(ctx, execution.SubmitRequest{
		Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: []execution.TaskInput{input},
	}); err != nil {
		t.Fatal(err)
	}
	traced := f.claim(host, container)
	if want := "00-" + sc.TraceID().String() + "-" + sc.SpanID().String() + "-01"; traced.TraceParent != want {
		t.Fatalf("traceparent %q, want %q", traced.TraceParent, want)
	}
	f.submit(1)
	if untraced := f.claim(host, container); untraced.TraceParent != "" {
		t.Fatalf("untraced submit carried %q", untraced.TraceParent)
	}
}
