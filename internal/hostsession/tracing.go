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
// trace. A start that waits for its image keeps it open across syncs, with
// wait open while it waits. The session goroutine alone uses it.
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

// waiting records that container's start waits for its image to convert in
// the work of kind traceparent names, linking the wait to that work.
func (sess *session) waiting(container execution.ContainerID, kind, traceparent string) {
	p := sess.starts[container]
	if p == nil {
		return
	}
	if p.wait == nil {
		_, p.wait = sess.server.tracer.Start(trace.ContextWithSpan(context.Background(), p.span), "hostsession.image_wait",
			trace.WithAttributes(attribute.String("lazycloud.wait", kind)))
	}
	if traceparent != "" && !p.linked[traceparent] && traceparent != telemetry.TraceParentOf(trace.ContextWithSpan(context.Background(), p.span)) {
		p.linked[traceparent] = true
		p.wait.AddLink(telemetry.Link(traceparent))
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
		p.span.SetStatus(codes.Error, failure)
	}
	p.span.End()
}

// endStarts ends the start spans of containers no longer derived, and all
// of them when derived is nil, as when the session ends.
func (sess *session) endStarts(derived map[execution.ContainerID]bool, failure string) {
	for container := range sess.starts {
		if !derived[container] {
			sess.endStart(container, failure)
		}
	}
}
