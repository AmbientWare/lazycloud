package hostsession

import (
	"context"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/observability"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// pendingStart is the span of one container's start on this session, from
// the first sync that derived it to the start sent, in the container's
// trace. A start that waits for its image keeps the span open across syncs,
// with a wait span open until it ends. Only the session goroutine uses it.
type pendingStart struct {
	span trace.Span
	// assigned is when placement gave the container this host.
	assigned *time.Time
	wait     trace.Span
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
		p = &pendingStart{span: span, assigned: start.AssignedAt, linked: map[string]bool{}}
		sess.starts[start.Container] = p
	}
	return trace.ContextWithSpan(ctx, p.span)
}

// waiting records that container's start waits for its image, converting
// in the work of kind that traceparent names, if any. The wait links to
// that work unless the work is in the start's own trace, as when this start
// began it.
func (sess *session) waiting(ctx context.Context, container execution.ContainerID, kind, traceparent string) {
	p := sess.starts[container]
	if p.wait == nil {
		_, p.wait = sess.server.tracer.Start(ctx, "hostsession.image_wait", trace.WithAttributes(attribute.String("lazycloud.wait", kind)))
	}
	work := telemetry.SpanContextOf(traceparent)
	if work.IsValid() && !p.linked[traceparent] && work.TraceID() != p.span.SpanContext().TraceID() {
		p.linked[traceparent] = true
		p.wait.AddLink(trace.Link{SpanContext: work})
	}
}

// endStart ends container's start span; a non-empty failure marks it
// failed. With record set, a start that waited for its image stores the
// wait as its conversion stage, from the container's assignment until now.
// The stage keeps its latest end, so a wait that spans sessions is stored
// whole when the last one waits too, and up to the end of the one before
// when the image is ready by the time the last reads it.
func (sess *session) endStart(ctx context.Context, container execution.ContainerID, failure string, record bool) {
	p := sess.starts[container]
	if p == nil {
		return
	}
	delete(sess.starts, container)
	if obs := sess.server.config.Observability; record && p.wait != nil && p.assigned != nil && obs != nil {
		stage := observability.StartupStage{Kind: observability.StageConversion, StartedAt: *p.assigned, FinishedAt: time.Now()}
		if err := obs.RecordStartup(ctx, sess.host, container, []observability.StartupStage{stage}); err != nil {
			sess.server.logger.WarnContext(ctx, "recording the conversion stage failed", "container_id", container.String(), "error", err)
		}
	}
	if p.wait != nil {
		p.wait.End()
	}
	if failure != "" {
		p.span.SetStatus(codes.Error, telemetry.Redact(failure))
	}
	p.span.End()
}

// endStarts ends the starts of containers no longer derived. With derived
// nil, as when the session ends, it ends all of them and records their
// waits so far.
func (sess *session) endStarts(ctx context.Context, derived map[execution.ContainerID]bool) {
	for container := range sess.starts {
		if !derived[container] {
			sess.endStart(ctx, container, "", derived == nil)
		}
	}
}
