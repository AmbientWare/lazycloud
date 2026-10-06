package telemetry

import (
	"context"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

// scope names the instrumentation of every span LazyCloud starts.
const scope = "github.com/AmbientWare/lazycloud"

// Work that outlives a request or pass carries its trace in durable rows and
// host commands as a W3C traceparent: a container, an image build and a
// platform image conversion each name the span that asked for them, and the
// spans of later steps, in any process, are its children. A step that joins
// work another trace started links to that trace instead.

// TracerOf is the tracer of ctx's span: owners trace under the provider of
// the request, RPC or pass that called them and hold none of their own.
// Without a recording span in ctx it records nothing.
func TracerOf(ctx context.Context) trace.Tracer {
	return trace.SpanFromContext(ctx).TracerProvider().Tracer(scope)
}

// Start starts a child of ctx's span with ctx's tracer.
func Start(ctx context.Context, name string, opts ...trace.SpanStartOption) (context.Context, trace.Span) {
	return TracerOf(ctx).Start(ctx, name, opts...) //nolint:spancheck // The caller ends it.
}

// StartIn starts a span with tracer as a child of the span traceparent
// names, or as a new root when traceparent is empty or malformed, never as
// a child of ctx's span.
func StartIn(ctx context.Context, tracer trace.Tracer, traceparent, name string, opts ...trace.SpanStartOption) (context.Context, trace.Span) {
	if sc := spanContextOf(traceparent); sc.IsValid() {
		ctx = trace.ContextWithRemoteSpanContext(ctx, sc)
	} else {
		opts = append(opts, trace.WithNewRoot())
	}
	return tracer.Start(ctx, name, opts...) //nolint:spancheck // The caller ends it.
}

// Record records a finished step from began to now as a child of ctx's
// span, for steps timed before their span could start.
func Record(ctx context.Context, name string, began time.Time, attrs ...attribute.KeyValue) {
	_, span := Start(ctx, name, trace.WithTimestamp(began), trace.WithAttributes(attrs...))
	span.End()
}

// LinkTo links a span to the one traceparent names; empty or malformed
// adds nothing.
func LinkTo(traceparent string) trace.SpanStartOption {
	if sc := spanContextOf(traceparent); sc.IsValid() {
		return trace.WithLinks(trace.Link{SpanContext: sc})
	}
	return trace.WithLinks()
}

// Link is a link to the span traceparent names; an empty or malformed one
// is invalid, and spans drop it.
func Link(traceparent string) trace.Link {
	return trace.Link{SpanContext: spanContextOf(traceparent)}
}

// Fail marks span failed with err, when err is not nil, and ends it.
func Fail(span trace.Span, err error) {
	if err != nil {
		span.RecordError(err)
		span.SetStatus(codes.Error, err.Error())
	}
	span.End()
}

func spanContextOf(traceparent string) trace.SpanContext {
	if traceparent == "" {
		return trace.SpanContext{}
	}
	ctx := propagation.TraceContext{}.Extract(context.Background(), propagation.MapCarrier{"traceparent": traceparent})
	return trace.SpanContextFromContext(ctx)
}

// Attribute keys of span attributes beside the correlation keys.
const (
	AttrImage     = "lazycloud.image"
	AttrLayer     = "lazycloud.layer"
	AttrBuild     = "lazycloud.build_id"
	AttrWorkspace = "lazycloud.workspace_id"
	AttrRelease   = "lazycloud.release_id"
)

// Container is the container_id attribute.
func Container(id string) attribute.KeyValue { return attribute.String("lazycloud."+KeyContainer, id) }

// Host is the host_id attribute.
func Host(id string) attribute.KeyValue { return attribute.String("lazycloud."+KeyHost, id) }

// Task is the task_id attribute.
func Task(id string) attribute.KeyValue { return attribute.String("lazycloud."+KeyTask, id) }
