package execution

import (
	"context"
	"testing"
	"time"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// A claimed task records its queue wait only in the trace it came with; a
// task without one, such as a cron task, starts no trace.
func TestClaimsRecordQueueWaitsOnlyInTheirTrace(t *testing.T) {
	spans := tracetest.NewSpanRecorder()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(spans))
	ctx, call := provider.Tracer("").Start(context.Background(), "ClaimTasks")
	defer call.End()
	stored := "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
	for _, parent := range []string{"", "not a traceparent", stored} {
		traceClaim(ctx, ClaimedTask{TraceParent: parent}, time.Now(), ContainerID{}, "")
	}
	ended := spans.Ended()
	if len(ended) != 1 || ended[0].Name() != "execution.queued" || ended[0].SpanContext().TraceID().String() != "0af7651916cd43dd8448eb211c80319c" {
		t.Fatalf("recorded %d queue waits, want one in the stored trace", len(ended))
	}
}
