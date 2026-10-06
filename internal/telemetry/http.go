package telemetry

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"log/slog"
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

// RequestIDHeader carries the request id the server assigns, so a caller can
// quote it when reporting a problem.
const RequestIDHeader = "X-Request-Id"

type routeKey struct{}

// route is filled by the operation the router dispatched to.
type route struct{ name string }

// SetRoute names the operation serving the request. Metrics and the span use
// it instead of the raw path, which holds ids.
func SetRoute(ctx context.Context, name string) {
	if r, ok := ctx.Value(routeKey{}).(*route); ok {
		r.name = name
	}
	trace.SpanFromContext(ctx).SetName(name)
}

// HTTPHandler traces next, assigns each request an id that its logs carry,
// and records its operation, status and duration on t's registry.
func (t *Telemetry) HTTPHandler(next http.Handler) http.Handler {
	duration := prometheus.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "lazycloud_http_request_duration_seconds",
		Help:    "Public API request duration by operation and status, up to the end of the response.",
		Buckets: []float64{0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 10, 60},
	}, []string{"operation", "status"})
	inFlight := prometheus.NewGauge(prometheus.GaugeOpts{
		Name: "lazycloud_http_requests_in_flight",
		Help: "Public API requests being served, including open streams.",
	})
	t.Registry.MustRegister(duration, inFlight)
	inner := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := newRequestID()
		w.Header().Set(RequestIDHeader, id)
		rt := &route{name: "unmatched"}
		ctx := context.WithValue(r.Context(), routeKey{}, rt)
		ctx = With(ctx, slog.String(KeyRequest, id))
		recorder := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		began := time.Now()
		inFlight.Inc()
		defer func() {
			inFlight.Dec()
			duration.WithLabelValues(rt.name, strconv.Itoa(recorder.status)).Observe(time.Since(began).Seconds())
		}()
		next.ServeHTTP(recorder, r.WithContext(ctx))
	})
	return otelhttp.NewHandler(inner, "http", t.publicTracing()...)
}

// EdgeHandler traces next, the edge serving workload traffic.
func (t *Telemetry) EdgeHandler(next http.Handler) http.Handler {
	return otelhttp.NewHandler(next, "edge", append(t.publicTracing(),
		otelhttp.WithSpanNameFormatter(func(_ string, r *http.Request) string { return "edge " + r.Method }))...)
}

// publicTracing traces requests anyone may send: a caller's trace context
// becomes a link, so callers cannot choose what is sampled or join their
// spans to ours.
func (t *Telemetry) publicTracing() []otelhttp.Option {
	return []otelhttp.Option{
		otelhttp.WithPublicEndpointFn(func(*http.Request) bool { return true }),
		otelhttp.WithTracerProvider(t.provider), otelhttp.WithPropagators(propagation.TraceContext{}),
	}
}

// statusRecorder keeps the status for metrics and passes flushes through,
// which streamed responses need.
type statusRecorder struct {
	http.ResponseWriter
	status      int
	wroteHeader bool
}

func (r *statusRecorder) WriteHeader(status int) {
	if !r.wroteHeader {
		r.status, r.wroteHeader = status, true
	}
	r.ResponseWriter.WriteHeader(status)
}

func (r *statusRecorder) Write(b []byte) (int, error) {
	r.wroteHeader = true
	return r.ResponseWriter.Write(b) //nolint:wrapcheck // A writer passes its delegate's error through.
}

// Unwrap lets http.ResponseController reach Flush and deadlines.
func (r *statusRecorder) Unwrap() http.ResponseWriter { return r.ResponseWriter }

func newRequestID() string {
	var b [12]byte
	_, _ = rand.Read(b[:])
	return hex.EncodeToString(b[:])
}
