package telemetry_test

import (
	"errors"
	"fmt"
	"net"
	"net/url"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
	oteltrace "go.opentelemetry.io/otel/trace"
	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

const presigned = "https://bucket.s3.amazonaws.com/layers/data?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeef"

// No exported span carries a presigned URL's query, whether a failed
// request's error, an error recorded by other instrumentation or an
// attribute holds it.
func TestSpansCarryNoPresignedQuery(t *testing.T) {
	lis, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	recv := &requests{got: make(chan *collector.ExportTraceServiceRequest, 4)}
	g := grpc.NewServer()
	collector.RegisterTraceServiceServer(g, recv)
	go func() { _ = g.Serve(lis) }()
	defer g.Stop()
	tel, err := telemetry.New(t.Context(), telemetry.Config{Service: "agent", OTLPEndpoint: lis.Addr().String(), OTLPInsecure: true, SampleRatio: 1})
	if err != nil {
		t.Fatal(err)
	}
	failed := fmt.Errorf("download source: %w", &url.Error{Op: "Get", URL: presigned, Err: errors.New("connection reset")})
	_, step := tel.Tracer().Start(t.Context(), "agent.source")
	telemetry.Fail(step, failed)
	_, other := tel.Tracer().Start(t.Context(), "GET", oteltrace.WithAttributes(attribute.String("url.full", presigned)))
	other.RecordError(failed)
	other.End()
	if err := tel.Shutdown(t.Context()); err != nil {
		t.Fatal(err)
	}
	var exported strings.Builder
	for len(recv.got) > 0 {
		exported.WriteString(protojson.Format(<-recv.got))
	}
	if !strings.Contains(exported.String(), "agent.source") || strings.Contains(exported.String(), "X-Amz-Signature") {
		t.Fatalf("exported spans hold a signature or lack the step:\n%s", exported.String())
	}
	var stripped *url.Error
	if redacted := telemetry.RedactURL(failed); !errors.As(redacted, &stripped) || strings.Contains(redacted.Error(), "X-Amz") ||
		!strings.Contains(redacted.Error(), "connection reset") {
		t.Fatalf("redacted error %v", redacted)
	}
}
