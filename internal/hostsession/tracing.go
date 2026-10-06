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

// startEnd is how a start left the session.
type startEnd int

const (
	// startDropped: its container no longer starts on the host.
	startDropped startEnd = iota
	// startDone: the start was sent, or failed for good.
	startDone
	// startPaused: the session ended while the start waited.
	startPaused
)

// endStart ends container's start span; a non-empty failure marks it
// failed. A start that waited for its image stores the wait as its
// conversion stage, from the container's assignment until now, unless it
// was dropped; the stage keeps its latest end. A start assigned before this
// session opened may have waited in an earlier one, so once done it moves
// the end of a stage stored then, and the stage runs until the start was
// sent or failed.
func (sess *session) endStart(ctx context.Context, container execution.ContainerID, failure string, end startEnd) {
	p := sess.starts[container]
	if p == nil {
		return
	}
	delete(sess.starts, container)
	if obs := sess.server.config.Observability; obs != nil && p.assigned != nil && end != startDropped {
		var err error
		now := time.Now()
		switch {
		case p.wait != nil:
			stage := observability.StartupStage{Kind: observability.StageConversion, StartedAt: *p.assigned, FinishedAt: now}
			err = obs.RecordStartup(ctx, sess.host, container, []observability.StartupStage{stage})
		case end == startDone && p.assigned.Before(sess.opened):
			err = obs.ExtendConversion(ctx, sess.host, container, now)
		}
		if err != nil {
			sess.server.logger.WarnContext(ctx, "recording the conversion stage failed", "container_id", container.String(), "error", err)
		}
	}
	if p.wait != nil {
		p.wait.End()
	}
	if failure != "" {
		p.span.SetStatus(codes.Error, failure)
	}
	p.span.End()
}

// endStarts ends the starts of containers no longer derived. With derived
// nil, as when the session ends, it ends all of them, paused.
func (sess *session) endStarts(ctx context.Context, derived map[execution.ContainerID]bool) {
	end := startDropped
	if derived == nil {
		end = startPaused
	}
	for container := range sess.starts {
		if !derived[container] {
			sess.endStart(ctx, container, "", end)
		}
	}
}
