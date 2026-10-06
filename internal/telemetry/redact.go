package telemetry

import (
	"context"
	"strings"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// redacting removes URL queries from every span it exports, whatever
// instrumentation recorded them: in string attributes, in event
// attributes such as an error's message, and in the status.
type redacting struct{ sdktrace.SpanExporter }

func (r redacting) ExportSpans(ctx context.Context, spans []sdktrace.ReadOnlySpan) error {
	out := make([]sdktrace.ReadOnlySpan, len(spans))
	for n, s := range spans {
		out[n] = redactedSpan{s}
	}
	return r.SpanExporter.ExportSpans(ctx, out) //nolint:wrapcheck // The exporter's error.
}

type redactedSpan struct{ sdktrace.ReadOnlySpan }

func (s redactedSpan) Attributes() []attribute.KeyValue {
	return redactAttrs(s.ReadOnlySpan.Attributes())
}

func (s redactedSpan) Status() sdktrace.Status {
	status := s.ReadOnlySpan.Status()
	status.Description = Redact(status.Description)
	return status
}

func (s redactedSpan) Events() []sdktrace.Event {
	events := s.ReadOnlySpan.Events()
	out := make([]sdktrace.Event, len(events))
	for n, e := range events {
		e.Attributes = redactAttrs(e.Attributes)
		out[n] = e
	}
	return out
}

// redactAttrs is attrs with URL queries removed from string values, copied
// only when one changes.
func redactAttrs(attrs []attribute.KeyValue) []attribute.KeyValue {
	var out []attribute.KeyValue
	for n, a := range attrs {
		if a.Value.Type() != attribute.STRING || !strings.Contains(a.Value.AsString(), "?") {
			continue
		}
		if out == nil {
			out = append([]attribute.KeyValue(nil), attrs...)
		}
		out[n] = attribute.String(string(a.Key), Redact(a.Value.AsString()))
	}
	if out == nil {
		return attrs
	}
	return out
}
