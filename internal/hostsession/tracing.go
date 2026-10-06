package hostsession

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// pendingStart is the span of one container's start on this session, from
// the first sync that derived it to the start sent, in the container's
// trace. A start that waits for its image keeps the span open across syncs,
// with a wait span open until the image is ready. Only the session goroutine
// uses it.
type pendingStart struct {
	span trace.Span
	wait trace.Span
	// linked holds the traces of the conversions the wait linked to.
	linked map[string]bool
}

// startContext returns ctx under the span of start's start, opening it on
// the first sync that derives the start.
func (sess *session) startContext(ctx context.Context, start execution.StartCommand) context.Context {
	p, ok := sess.starts[start.Container]
	if !ok {
		_, span := telemetry.StartIn(ctx, sess.server.tracer, start.Traceparent, "hostsession.start", trace.WithAttributes(
			telemetry.Container(start.Container.String()), telemetry.Host(sess.host.String())))
		p = &pendingStart{span: span, linked: map[string]bool{}}
		sess.starts[start.Container] = p
	}
	return trace.ContextWithSpan(ctx, p.span)
}

// waiting records that container's start waits for its image, converting
// in the work of kind that traceparent names. The wait links to that work
// unless the work is in the start's own trace, as when this start began it.
func (sess *session) waiting(ctx context.Context, container execution.ContainerID, kind, traceparent string) {
	p := sess.starts[container]
	if p == nil {
		return
	}
	if p.wait == nil {
		_, p.wait = sess.server.tracer.Start(trace.ContextWithSpan(ctx, p.span), "hostsession.image_wait",
			trace.WithAttributes(attribute.String("lazycloud.wait", kind)))
	}
	work := telemetry.SpanContextOf(traceparent)
	if work.IsValid() && !p.linked[traceparent] && work.TraceID() != p.span.SpanContext().TraceID() {
		p.linked[traceparent] = true
		p.wait.AddLink(trace.Link{SpanContext: work})
	}
}

// endStart ends container's start span; a non-empty failure marks it
// failed.
func (sess *session) endStart(container execution.ContainerID, failure string) {
	p := sess.starts[container]
	if p == nil {
		return
	}
	delete(sess.starts, container)
	if p.wait != nil {
		p.wait.End()
	}
	if failure != "" {
		p.span.SetStatus(codes.Error, telemetry.Redact(failure))
	}
	p.span.End()
}

// endStarts ends the start spans of containers no longer derived, and all
// of them when derived is nil, as when the session ends.
func (sess *session) endStarts(derived map[execution.ContainerID]bool) {
	for container := range sess.starts {
		if !derived[container] {
			sess.endStart(container, "")
		}
	}
}
