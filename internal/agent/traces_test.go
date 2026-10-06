package agent

import (
	"context"
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"

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

// An OTLP export to the host's trace socket reaches the session as the
// batch the exporter sent.
func TestHostSpansReachTheSession(t *testing.T) {
	dir, err := os.MkdirTemp("", "lctrace")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	socket := filepath.Join(dir, "t.sock")
	ctx, cancel := context.WithCancel(t.Context())
	out := &sessionOut{ch: make(chan *hostproto.HostMessage, 8), cancel: func() {}}
	a := &Agent{cfg: Config{TraceSocket: socket}, log: slog.New(slog.DiscardHandler), session: out}
	served := make(chan struct{})
	go func() {
		defer close(served)
		a.serveTraces(ctx)
	}()
	t.Cleanup(func() { cancel(); <-served })

	conn, err := grpc.NewClient("unix://"+socket, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	request := &collector.ExportTraceServiceRequest{ResourceSpans: []*tracepb.ResourceSpans{{ScopeSpans: []*tracepb.ScopeSpans{{
		Spans: []*tracepb.Span{{TraceId: make([]byte, 16), SpanId: make([]byte, 8), Name: "agent.start"}},
	}}}}}
	for deadline := time.Now().Add(10 * time.Second); ; time.Sleep(20 * time.Millisecond) {
		_, err := collector.NewTraceServiceClient(conn).Export(t.Context(), request)
		if err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal(err)
		}
	}
	var got collector.ExportTraceServiceRequest
	if err := proto.Unmarshal((<-out.ch).GetTraces().GetOtlp(), &got); err != nil || !proto.Equal(&got, request) {
		t.Fatalf("the session carried %v (%v), want the exported batch", &got, err)
	}
}
