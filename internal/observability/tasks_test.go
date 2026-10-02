package observability_test

import (
	"errors"
	"log/slog"
	"testing"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/trace"

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
