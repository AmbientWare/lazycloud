package execution

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"

	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// rolloutTrace is how long after a release is created its first containers
// join the deploy's trace.
const rolloutTrace = 10 * time.Minute

// traceparent is the trace of ctx's span, stored with a task or container
// so the steps other processes take for it join that trace; nil when the
// span was not sampled.
func traceparent(ctx context.Context) *string {
	if tp := telemetry.TraceParentOf(ctx); tp != "" {
		return &tp
	}
	return nil
}

// scaleUp starts the span of a planning decision to create count
// containers of release, in the trace of the demand that asked for them:
// the longest-waiting queued task, else the release's deploy when rollout
// says the release has no live container and no demand, so its warm
// minimum alone asks for them. The containers store the span's
// traceparent. Without tracing it reads no trace.
func (e *Execution) scaleUp(ctx context.Context, q *Queries, release uuid.UUID, count int, rollout bool) (context.Context, trace.Span, error) {
	var parent string
	if trace.SpanContextFromContext(ctx).IsValid() {
		var err error
		parent, err = q.ScaleUpTrace(ctx, ScaleUpTraceParams{ReleaseID: release, Rollout: rollout, RolloutSeconds: rolloutTrace.Seconds()})
		if err != nil {
			return ctx, nil, fmt.Errorf("read the scale-up's trace: %w", err)
		}
	}
	ctx, span := telemetry.StartFor(ctx, parent, "execution.scale_up",
		trace.WithAttributes(attribute.String(telemetry.AttrRelease, release.String()), attribute.Int("lazycloud.containers", count)))
	return ctx, span, nil
}
