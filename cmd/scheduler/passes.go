package main

import (
	"context"
	"log/slog"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// newPassTimer returns a wrapper that records each pass's duration by name.
// The pass runs under a span the sampler drops, which gives its steps their
// tracer: a step for traced work records in that work's trace, and a
// scale-up, launch or resume that no traced work asked for starts its own.
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

// watch is how a loop's passes are watched.
type watch int

const (
	// unwatched passes are neither timed nor tracked.
	unwatched watch = iota
	// timedOnly passes record their duration but have no deadline and no
	// heartbeat: they wait on AWS for as long as it takes.
	timedOnly
	// tracked passes are timed, run under the pass deadline and count for
	// /healthz and /readyz.
	tracked
)

// loop is one scheduler loop: a pass run on its cadence and its wakes.
type loop struct {
	name       string
	cadence    cadence
	wake, also <-chan struct{}
	watch      watch
	pass       func(context.Context) bool
}

// step is one call of a pass. A failure is logged as what unless the pass
// was cancelled, as a warning when the work finishes on a later pass, and
// the pass's next step still runs.
type step struct {
	what string
	warn bool
	do   func(context.Context, *slog.Logger) error
}

// steps is a pass that runs each step in order and never asks to rerun.
func steps(logger *slog.Logger, all ...step) func(context.Context) bool {
	return func(ctx context.Context) bool {
		for _, s := range all {
			err := s.do(ctx, logger)
			switch {
			case err == nil, ctx.Err() != nil:
			case s.warn:
				logger.WarnContext(ctx, s.what, "error", err)
			default:
				logger.ErrorContext(ctx, s.what, "error", err)
			}
		}
		return false
	}
}

// discardContext drops the count of a step that takes no logger.
func discardContext[T any](do func(context.Context) (T, error)) func(context.Context, *slog.Logger) error {
	return func(ctx context.Context, _ *slog.Logger) error {
		_, err := do(ctx)
		return err
	}
}

// discard drops a step's count.
func discard[T any](do func(context.Context, *slog.Logger) (T, error)) func(context.Context, *slog.Logger) error {
	return func(ctx context.Context, logger *slog.Logger) error {
		_, err := do(ctx, logger)
		return err
	}
}
