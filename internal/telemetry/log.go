package telemetry

import (
	"context"
	"io"
	"log/slog"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
)

// Correlation keys shared by every binary's logs and span attributes, so one
// task, attempt, container, host or request can be followed across the
// server, scheduler and agent.
const (
	KeyTask      = "task_id"
	KeyAttempt   = "attempt_id"
	KeyContainer = "container_id"
	KeyHost      = "host_id"
	KeyRequest   = "request_id"
)

type fieldsKey struct{}

// With returns ctx carrying correlation fields that every record logged with
// it includes. Later values of a key replace earlier ones. When ctx holds a
// recording span the fields become its attributes too.
func With(ctx context.Context, fields ...slog.Attr) context.Context {
	if len(fields) == 0 {
		return ctx
	}
	prev, _ := ctx.Value(fieldsKey{}).([]slog.Attr)
	merged := make([]slog.Attr, 0, len(prev)+len(fields))
	for _, p := range prev {
		replaced := false
		for _, f := range fields {
			if f.Key == p.Key {
				replaced = true
				break
			}
		}
		if !replaced {
			merged = append(merged, p)
		}
	}
	merged = append(merged, fields...)
	if span := trace.SpanFromContext(ctx); span.IsRecording() {
		attrs := make([]attribute.KeyValue, 0, len(fields))
		for _, f := range fields {
			attrs = append(attrs, attribute.String("lazycloud."+f.Key, f.Value.String()))
		}
		span.SetAttributes(attrs...)
	}
	return context.WithValue(ctx, fieldsKey{}, merged)
}

// Fields returns the correlation fields ctx carries.
func Fields(ctx context.Context) []slog.Attr {
	fields, _ := ctx.Value(fieldsKey{}).([]slog.Attr)
	return fields
}

// LogFormat selects the record encoding.
type LogFormat string

const (
	LogText LogFormat = "text"
	LogJSON LogFormat = "json"
)

// NewLogger writes records in format to w. Records logged with a context
// carry its correlation fields and, inside a span, trace_id and span_id.
func NewLogger(w io.Writer, format LogFormat, service string) *slog.Logger {
	var base slog.Handler
	switch format {
	case LogJSON:
		base = slog.NewJSONHandler(w, nil)
	case LogText:
		base = slog.NewTextHandler(w, nil)
	default:
		base = slog.NewTextHandler(w, nil)
	}
	return slog.New(correlated{next: base}).With("service", service)
}

// correlated adds the context's correlation fields to each record.
type correlated struct{ next slog.Handler }

func (h correlated) Enabled(ctx context.Context, level slog.Level) bool {
	return h.next.Enabled(ctx, level)
}

func (h correlated) Handle(ctx context.Context, record slog.Record) error {
	if ctx != nil {
		record.AddAttrs(Fields(ctx)...)
		if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
			record.AddAttrs(slog.String("trace_id", sc.TraceID().String()), slog.String("span_id", sc.SpanID().String()))
		}
	}
	return h.next.Handle(ctx, record) //nolint:wrapcheck // A handler passes its delegate's error through.
}

func (h correlated) WithAttrs(attrs []slog.Attr) slog.Handler {
	return correlated{next: h.next.WithAttrs(attrs)}
}

func (h correlated) WithGroup(name string) slog.Handler {
	return correlated{next: h.next.WithGroup(name)}
}
