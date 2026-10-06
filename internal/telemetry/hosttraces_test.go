package telemetry_test

import (
	"context"
	"log/slog"
	"net"
	"testing"
	"time"

	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
	tracepb "go.opentelemetry.io/proto/otlp/trace/v1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"

	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// requests is an OTLP collector that keeps whole requests.
type requests struct {
	collector.UnimplementedTraceServiceServer
	got chan *collector.ExportTraceServiceRequest
}

func (r *requests) Export(_ context.Context, req *collector.ExportTraceServiceRequest) (*collector.ExportTraceServiceResponse, error) {
	r.got <- req
	return &collector.ExportTraceServiceResponse{}, nil
}

// A host's spans reach the collector marked with the host that sent them,
// a host cannot make them look like the control plane's or carry AWS
// metadata, and a host past its span budget is dropped.
func TestHostSpansCarryTheirHost(t *testing.T) {
	lis, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	recv := &requests{got: make(chan *collector.ExportTraceServiceRequest, 1)}
	g := grpc.NewServer()
	collector.RegisterTraceServiceServer(g, recv)
	go func() { _ = g.Serve(lis) }()
	defer g.Stop()
	tel, err := telemetry.New(t.Context(), telemetry.Config{Service: "server", OTLPEndpoint: lis.Addr().String(), OTLPInsecure: true, SampleRatio: 1})
	if err != nil {
		t.Fatal(err)
	}
	forward, err := tel.HostTraces(slog.New(slog.DiscardHandler))
	if err != nil {
		t.Fatal(err)
	}
	ctx, stop := context.WithCancel(t.Context())
	done := make(chan error, 1)
	go func() { done <- forward.Run(ctx) }()
	str := func(k, v string) *commonpb.KeyValue {
		return &commonpb.KeyValue{Key: k, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: v}}}
	}
	raw, err := proto.Marshal(&collector.ExportTraceServiceRequest{ResourceSpans: []*tracepb.ResourceSpans{{
		Resource: &resourcepb.Resource{Attributes: []*commonpb.KeyValue{str("service.name", "lazycloud-server"), str("lazycloud.host_id", "someone-else")}},
		ScopeSpans: []*tracepb.ScopeSpans{{Spans: []*tracepb.Span{{Name: "agent.start", TraceId: make([]byte, 16), SpanId: make([]byte, 8),
			Attributes: []*commonpb.KeyValue{
				str("lazycloud.host_id", "someone-else"), str("aws.xray.annotations", "x"), str("peer.service", "lazycloud-server"),
				str("service.name", "lazycloud-server"), str("lazycloud.layers", "4"),
			}}}}},
	}}})
	if err != nil {
		t.Fatal(err)
	}
	forward.Offer("host-1", raw)
	var got *collector.ExportTraceServiceRequest
	select {
	case got = <-recv.got:
	case <-time.After(10 * time.Second):
		t.Fatal("the collector received nothing")
	}
	stop()
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	attrs := map[string]string{}
	for _, a := range got.GetResourceSpans()[0].GetResource().GetAttributes() {
		attrs[a.GetKey()] = a.GetValue().GetStringValue()
	}
	if attrs["service.name"] != "lazycloud-agent" || attrs["lazycloud.host_id"] != "host-1" || len(attrs) != 3 {
		t.Fatalf("forwarded resource %v", attrs)
	}
	spans := got.GetResourceSpans()[0].GetScopeSpans()[0].GetSpans()
	if len(spans) != 1 || spans[0].GetName() != "agent.start" {
		t.Fatalf("forwarded spans %v", spans)
	}
	attrs = map[string]string{}
	for _, a := range spans[0].GetAttributes() {
		attrs[a.GetKey()] = a.GetValue().GetStringValue()
	}
	if len(attrs) != 2 || attrs["lazycloud.host_id"] != "host-1" || attrs["lazycloud.layers"] != "4" {
		t.Fatalf("forwarded span attributes %v", attrs)
	}

	// A batch past the host's budget is dropped and counted.
	flood := &collector.ExportTraceServiceRequest{ResourceSpans: []*tracepb.ResourceSpans{{ScopeSpans: []*tracepb.ScopeSpans{{}}}}}
	for range 2500 {
		flood.ResourceSpans[0].ScopeSpans[0].Spans = append(flood.ResourceSpans[0].ScopeSpans[0].Spans, &tracepb.Span{Name: "x"})
	}
	raw, err = proto.Marshal(flood)
	if err != nil {
		t.Fatal(err)
	}
	forward.Offer("host-1", raw)
	families, err := tel.Registry.Gather()
	if err != nil {
		t.Fatal(err)
	}
	dropped := 0.0
	for _, f := range families {
		if f.GetName() == "lazycloud_host_spans_dropped_total" {
			dropped = f.GetMetric()[0].GetCounter().GetValue()
		}
	}
	if dropped != 2500 {
		t.Fatalf("dropped %v spans past the budget, want 2500", dropped)
	}
}
