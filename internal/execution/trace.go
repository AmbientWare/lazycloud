package execution

import (
	"context"

	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// traceparent is the submitting request's trace, stored with each task so
// the host's spans for its attempts join it; nil when the request was not
// traced.
func traceparent(ctx context.Context) *string {
	if tp := telemetry.TraceParentOf(ctx); tp != "" {
		return &tp
	}
	return nil
}
