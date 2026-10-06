package main

import (
	"context"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// newPassTimer returns a wrapper that records each pass's duration by name
// and runs it under a span the sampler never keeps, which hands the pass's
// steps the tracer that records them in their containers' traces.
func newPassTimer(tel *telemetry.Telemetry) func(name string, pass func(context.Context) bool) func(context.Context) bool {
	duration := prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "lazycloud_scheduler_pass_seconds",
		Help:    "Duration of one scheduler pass by loop.",
		Buckets: prometheus.ExponentialBuckets(0.0002, 3, 12),
	}, []string{"pass"})
	contended := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "lazycloud_scheduler_pass_reruns_total",
		Help: "Passes that found their lock held or work left and ran again at once.",
	}, []string{"pass"})
	tel.Registry.MustRegister(duration, contended)
	tracer := tel.Tracer()
	return func(name string, pass func(context.Context) bool) func(context.Context) bool {
		observe := duration.WithLabelValues(name)
		rerun := contended.WithLabelValues(name)
		return func(ctx context.Context) bool {
			ctx, span := tracer.Start(ctx, telemetry.PassPrefix+name)
			began := time.Now()
			again := pass(ctx)
			observe.Observe(time.Since(began).Seconds())
			span.End()
			if again {
				rerun.Inc()
			}
			return again
		}
	}
}
