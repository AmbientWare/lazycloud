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

// Request spans named by their operation export over OTLP, linked to the
// caller's trace, and /metrics reports the request.
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
		telemetry.SetRoute(r.Context(), "getTask")
		w.WriteHeader(http.StatusTeapot)
	}), tel.NewHTTPMetrics())
	server := httptest.NewServer(handler)
	defer server.Close()
	traceID := "4bf92f3577b34da6a3ce929d0e0e4736"
	req, err := http.NewRequestWithContext(t.Context(), "GET", server.URL+"/v1/workspaces/acme/tasks/x", nil)
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
	if err := tel.Shutdown(t.Context()); err != nil {
		t.Fatal(err)
	}
	recv.mu.Lock()
	spans := recv.spans
	recv.mu.Unlock()
	// The caller's trace is a link, not the parent: a public caller does
	// not decide sampling.
	if len(spans) != 1 || spans[0].GetName() != "getTask" || hex.EncodeToString(spans[0].GetTraceId()) == traceID ||
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
	if !strings.Contains(body, `lazycloud_http_request_duration_seconds_count{operation="getTask",status="418"} 1`) {
		t.Fatalf("metrics lack the request:\n%s", body)
	}
	stop()
	if err := <-served; err != nil {
		t.Fatal(err)
	}
}
