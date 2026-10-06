package telemetry_test

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/grpc"

	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// Records logged with a context carry its correlation fields and the
// active trace, whichever binary logs them.
func TestLogsCarryCorrelationFieldsAndTrace(t *testing.T) {
	var out bytes.Buffer
	logger := telemetry.NewLogger(&out, telemetry.LogJSON, "agent")
	sc := trace.NewSpanContext(trace.SpanContextConfig{TraceID: trace.TraceID{9}, SpanID: trace.SpanID{8}, TraceFlags: trace.FlagsSampled})
	ctx := trace.ContextWithSpanContext(t.Context(), sc)
	ctx = telemetry.With(ctx, slog.String(telemetry.KeyTask, "t1"), slog.String(telemetry.KeyContainer, "c1"))
	ctx = telemetry.With(ctx, slog.String(telemetry.KeyTask, "t2"))
	logger.InfoContext(ctx, "attempt finished", "attempt_id", "a1")
	var record map[string]any
	if err := json.Unmarshal(out.Bytes(), &record); err != nil {
		t.Fatal(err)
	}
	for key, want := range map[string]string{
		"service": "agent", "task_id": "t2", "container_id": "c1", "attempt_id": "a1",
		"trace_id": sc.TraceID().String(), "span_id": sc.SpanID().String(),
	} {
		if record[key] != want {
			t.Fatalf("%s = %v, want %s in %s", key, record[key], want, out.String())
		}
	}
}

// receiver is an in-process OTLP trace collector.
type receiver struct {
	collector.UnimplementedTraceServiceServer
	mu    sync.Mutex
	spans []*tracepb.Span
}

func (r *receiver) Export(_ context.Context, req *collector.ExportTraceServiceRequest) (*collector.ExportTraceServiceResponse, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	for _, rs := range req.GetResourceSpans() {
		for _, ss := range rs.GetScopeSpans() {
			r.spans = append(r.spans, ss.GetSpans()...)
		}
	}
	return &collector.ExportTraceServiceResponse{}, nil
}

// An API write's span, named by its operation, exports over OTLP linked to
// the caller's trace. Reads and the task long poll take the edge's ratio,
// none here. /metrics reports every request.
func TestRequestsExportSpansAndMetrics(t *testing.T) {
	lis, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	recv := &receiver{}
	g := grpc.NewServer()
	collector.RegisterTraceServiceServer(g, recv)
	go func() { _ = g.Serve(lis) }()
	defer g.Stop()

	metricsLis, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	metricsAddr := metricsLis.Addr().String()
	_ = metricsLis.Close()
	tel, err := telemetry.New(t.Context(), telemetry.Config{
		Service: "server", OTLPEndpoint: lis.Addr().String(), OTLPInsecure: true, SampleRatio: 1, MetricsAddr: metricsAddr,
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, stop := context.WithCancel(t.Context())
	served := make(chan error, 1)
	go func() { served <- tel.ServeMetrics(ctx, slog.New(slog.DiscardHandler)) }()

	handler := tel.HTTPHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		telemetry.SetRoute(r.Context(), "tasks")
		w.WriteHeader(http.StatusTeapot)
	}), tel.NewHTTPMetrics())
	server := httptest.NewServer(handler)
	defer server.Close()
	traceID := "4bf92f3577b34da6a3ce929d0e0e4736"
	for _, call := range []struct{ method, path string }{
		{"POST", "/v1/workspaces/acme/tasks"}, {"GET", "/v1/workspaces/acme/tasks/x"}, {"POST", "/v1/workspaces/acme/tasks/wait"},
	} {
		req, err := http.NewRequestWithContext(t.Context(), call.method, server.URL+call.path, nil)
		if err != nil {
			t.Fatal(err)
		}
		req.Header.Set("traceparent", "00-"+traceID+"-00f067aa0ba902b7-01")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusTeapot || len(resp.Header.Get(telemetry.RequestIDHeader)) != 24 {
			t.Fatalf("response %d, request id %q", resp.StatusCode, resp.Header.Get(telemetry.RequestIDHeader))
		}
	}
	if err := tel.Shutdown(t.Context()); err != nil {
		t.Fatal(err)
	}
	recv.mu.Lock()
	spans := recv.spans
	recv.mu.Unlock()
	// The caller's trace is a link, not the parent: a public caller does
	// not decide sampling.
	if len(spans) != 1 || spans[0].GetName() != "tasks" || hex.EncodeToString(spans[0].GetTraceId()) == traceID ||
		len(spans[0].GetLinks()) != 1 || hex.EncodeToString(spans[0].GetLinks()[0].GetTraceId()) != traceID {
		t.Fatalf("exported spans %v", spans)
	}

	var body string
	deadline := time.Now().Add(5 * time.Second)
	for {
		resp, err := http.Get("http://" + metricsAddr + "/metrics") //nolint:noctx // A local test listener.
		if err == nil {
			raw, _ := io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			body = string(raw)
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("metrics: %v", err)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !strings.Contains(body, `lazycloud_http_request_duration_seconds_count{operation="tasks",status="418"} 3`) {
		t.Fatalf("metrics lack the request:\n%s", body)
	}
	stop()
	if err := <-served; err != nil {
		t.Fatal(err)
	}
}

// A step started from a stored traceparent is a child in that trace. A
// gRPC call outside any traced step, a scheduler pass and a workload
// request past the edge's ratio start no trace, while a pass's step for a
// traced container records in that container's trace.
func TestStoredTraceparentsJoinTheirTrace(t *testing.T) {
	lis, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	recv := &receiver{}
	g := grpc.NewServer()
	collector.RegisterTraceServiceServer(g, recv)
	go func() { _ = g.Serve(lis) }()
	defer g.Stop()
	tel, err := telemetry.New(t.Context(), telemetry.Config{Service: "server", OTLPEndpoint: lis.Addr().String(), OTLPInsecure: true, SampleRatio: 1})
	if err != nil {
		t.Fatal(err)
	}
	ctx, root := tel.Tracer().Start(t.Context(), "submit")
	stored := telemetry.TraceParentOf(ctx)
	root.End()
	_, child := telemetry.StartIn(t.Context(), tel.Tracer(), stored, "agent.start")
	child.End()
	_, call := tel.Tracer().Start(t.Context(), "lazycloud.host.v1.HostService/ClaimTasks",
		trace.WithSpanKind(trace.SpanKindClient), trace.WithAttributes(attribute.String("rpc.system.name", "grpc")))
	call.End()
	passCtx, pass := tel.Tracer().Start(t.Context(), telemetry.PassPrefix+"place")
	_, idle := telemetry.Start(passCtx, "scheduling.idle")
	idle.End()
	_, placed := telemetry.StartFor(passCtx, stored, "scheduling.placement")
	placed.End()
	pass.End()
	server := httptest.NewServer(tel.EdgeHandler(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {})))
	resp, err := http.Get(server.URL) //nolint:noctx // A local test server.
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	server.Close()
	if err := tel.Shutdown(t.Context()); err != nil {
		t.Fatal(err)
	}
	recv.mu.Lock()
	defer recv.mu.Unlock()
	if len(recv.spans) != 3 {
		t.Fatalf("exported %d spans, want submit, agent.start and scheduling.placement: %v", len(recv.spans), recv.spans)
	}
	byName := map[string]*tracepb.Span{}
	for _, s := range recv.spans {
		byName[s.GetName()] = s
	}
	submit, start, placement := byName["submit"], byName["agent.start"], byName["scheduling.placement"]
	if submit == nil || start == nil || placement == nil || !bytes.Equal(start.GetTraceId(), submit.GetTraceId()) ||
		!bytes.Equal(start.GetParentSpanId(), submit.GetSpanId()) || !bytes.Equal(placement.GetTraceId(), submit.GetTraceId()) {
		t.Fatalf("agent.start and scheduling.placement are not in submit's trace: %v", recv.spans)
	}
}
