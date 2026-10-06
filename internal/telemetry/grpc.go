package telemetry

import (
	"context"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
)

// GRPCServerOption traces host calls and continues the trace an agent sent.
func (t *Telemetry) GRPCServerOption() grpc.ServerOption {
	return grpc.StatsHandler(otelgrpc.NewServerHandler(
		otelgrpc.WithTracerProvider(t.provider), otelgrpc.WithPropagators(propagation.TraceContext{})))
}

// GRPCDialOption traces calls to the server and sends their trace context.
func (t *Telemetry) GRPCDialOption() grpc.DialOption {
	return grpc.WithStatsHandler(otelgrpc.NewClientHandler(
		otelgrpc.WithTracerProvider(t.provider), otelgrpc.WithPropagators(propagation.TraceContext{})))
}

// TraceParentOf is the W3C traceparent of ctx's span, or "" outside a
// sampled span. It lets a durable row carry the trace its work belongs to.
func TraceParentOf(ctx context.Context) string {
	if !trace.SpanContextFromContext(ctx).IsSampled() {
		return ""
	}
	carrier := propagation.MapCarrier{}
	propagation.TraceContext{}.Inject(ctx, carrier)
	return carrier.Get("traceparent")
}

// WithTraceParent returns ctx whose remote parent is traceparent, so a span
// started from it joins that trace. An empty or malformed value returns ctx.
func WithTraceParent(ctx context.Context, traceparent string) context.Context {
	if traceparent == "" {
		return ctx
	}
	return propagation.TraceContext{}.Extract(ctx, propagation.MapCarrier{"traceparent": traceparent})
}
