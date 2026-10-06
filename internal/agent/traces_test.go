package agent

import (
	"context"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// A container API call that names a running attempt's task runs under the
// attempt's span, so the server's handling of it joins the attempt's
// trace; one naming another task runs under none.
func TestContainerAPICallsRunUnderTheirAttempt(t *testing.T) {
	_, span := sdktrace.NewTracerProvider().Tracer("").Start(context.Background(), "attempt")
	defer span.End()
	c := &container{traces: map[string]attemptTrace{"attempt-1": {task: "task-1", span: span}}}
	head := func(task string) *hostproto.APIRequestHead {
		return &hostproto.APIRequestHead{Headers: []*hostproto.APIHeader{{Name: "lazycloud-task", Value: task}}}
	}
	if got := trace.SpanContextFromContext(c.taskContext(t.Context(), taskOf(head("task-1")))); !got.Equal(span.SpanContext()) {
		t.Fatalf("the call of task-1 runs under %v, want the attempt's %v", got, span.SpanContext())
	}
	if got := trace.SpanContextFromContext(c.taskContext(t.Context(), taskOf(head("task-2")))); got.IsValid() {
		t.Fatalf("the call of another task runs under %v", got)
	}
}
