// Package telemetry gives each binary its tracer, its correlated logger and
// its Prometheus registry. Tracing exports over OTLP only when an endpoint is
// configured; otherwise spans are never recorded. Nothing here is global:
// each binary builds one Telemetry in main and passes it down.
package telemetry

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
	"go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"
)

// Config selects what a binary exports.
type Config struct {
	// Service names the binary in traces: server, scheduler, agent or
	// snapshotter.
	Service string
	Version string
	// OTLPEndpoint is an OTLP/gRPC collector address, host:port. Empty
	// turns tracing off.
	OTLPEndpoint string
	// OTLPInsecure sends spans without TLS, for a local collector.
	OTLPInsecure bool
	// SampleRatio is the share of new traces recorded, 0 to 1. A trace
	// that arrives sampled from another process is always recorded.
	SampleRatio float64
	// EdgeSampleRatio is the share of workload requests through the edge
	// traced, which anyone may send.
	EdgeSampleRatio float64
	// MetricsAddr is where /metrics listens. Empty serves nothing.
	MetricsAddr string
}

// Telemetry is one binary's tracer provider, propagator and metrics.
type Telemetry struct {
	cfg        Config
	provider   trace.TracerProvider
	shutdown   func(context.Context) error
	propagator propagation.TextMapPropagator
	// Registry holds the binary's Prometheus collectors.
	Registry *prometheus.Registry
}

// New builds the binary's telemetry. With no OTLP endpoint the tracer
// provider is a no-op, so instrumented code costs a context lookup.
func New(ctx context.Context, cfg Config) (*Telemetry, error) {
	registry := prometheus.NewRegistry()
	registry.MustRegister(collectors.NewGoCollector(), collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}))
	t := &Telemetry{
		cfg:        cfg,
		provider:   noop.NewTracerProvider(),
		shutdown:   func(context.Context) error { return nil },
		propagator: propagation.TraceContext{},
		Registry:   registry,
	}
	if cfg.OTLPEndpoint == "" {
		return t, nil
	}
	options := []otlptracegrpc.Option{otlptracegrpc.WithEndpoint(cfg.OTLPEndpoint)}
	if cfg.OTLPInsecure {
		options = append(options, otlptracegrpc.WithInsecure())
	}
	exporter, err := otlptracegrpc.New(ctx, options...)
	if err != nil {
		return nil, fmt.Errorf("create otlp exporter: %w", err)
	}
	res, err := resource.Merge(resource.Default(), resource.NewWithAttributes(semconv.SchemaURL,
		semconv.ServiceName("lazycloud-"+cfg.Service), semconv.ServiceVersion(cfg.Version)))
	if err != nil {
		return nil, fmt.Errorf("describe telemetry resource: %w", err)
	}
	provider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(redacting{exporter}),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.ParentBased(rootSampler{
			ratio: sdktrace.TraceIDRatioBased(cfg.SampleRatio), edge: sdktrace.TraceIDRatioBased(cfg.EdgeSampleRatio),
		})),
	)
	t.provider = provider
	t.shutdown = provider.Shutdown
	return t, nil
}

// Tracer is the binary's tracer for its own spans.
func (t *Telemetry) Tracer() trace.Tracer {
	return t.provider.Tracer(scope)
}

// Shutdown flushes buffered spans.
func (t *Telemetry) Shutdown(ctx context.Context) error {
	if err := t.shutdown(ctx); err != nil {
		return fmt.Errorf("flush spans: %w", err)
	}
	return nil
}

// ServeMetrics serves /metrics on cfg.MetricsAddr until ctx ends. Without an
// address it returns at once.
func (t *Telemetry) ServeMetrics(ctx context.Context, logger *slog.Logger) error {
	if t.cfg.MetricsAddr == "" {
		return nil
	}
	mux := http.NewServeMux()
	mux.Handle("GET /metrics", promhttp.HandlerFor(t.Registry, promhttp.HandlerOpts{Registry: t.Registry}))
	var lc net.ListenConfig
	listener, err := lc.Listen(ctx, "tcp", t.cfg.MetricsAddr)
	if err != nil {
		return fmt.Errorf("listen for metrics on %s: %w", t.cfg.MetricsAddr, err)
	}
	server := &http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	logger.InfoContext(ctx, "serving metrics", "addr", listener.Addr().String())
	done := make(chan error, 1)
	go func() { done <- server.Serve(listener) }()
	select {
	case <-ctx.Done():
		shutdownCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 2*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdownCtx)
		<-done
		return nil
	case err := <-done:
		if errors.Is(err, http.ErrServerClosed) {
			return nil
		}
		return fmt.Errorf("serve metrics: %w", err)
	}
}
