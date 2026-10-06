package telemetry

import (
	"net/http"
	"strings"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
	"go.opentelemetry.io/otel/trace"
)

// PassPrefix starts the name of a scheduler pass's span. A pass never
// starts a trace. Its steps for a container record in that container's
// trace, and a step for no container starts its own.
const PassPrefix = "scheduler."

// rootSampler decides the traces a binary starts. Workload requests through
// the edge, which anyone may send, and API reads, which dashboards and SDKs
// poll, take edge's share; every other trace, the API writes that submit
// tasks, deploy and build among them, takes ratio's. It starts none for a
// scheduler pass or for a gRPC call outside a traced step: hosts call the
// server all the time, so a call records only in the trace of the step
// that made it.
type rootSampler struct{ ratio, edge sdktrace.Sampler }

func (s rootSampler) ShouldSample(p sdktrace.SamplingParameters) sdktrace.SamplingResult {
	if strings.HasPrefix(p.Name, PassPrefix) {
		return s.drop(p)
	}
	if strings.HasPrefix(p.Name, "edge") {
		return s.edge.ShouldSample(p)
	}
	var method, path string
	for _, a := range p.Attributes {
		switch a.Key {
		case semconv.RPCSystemNameKey:
			return s.drop(p)
		case semconv.HTTPRequestMethodKey:
			method = a.Value.AsString()
		case semconv.URLPathKey:
			path = a.Value.AsString()
		}
	}
	if polled(method, path) {
		return s.edge.ShouldSample(p)
	}
	return s.ratio.ShouldSample(p)
}

func (rootSampler) drop(p sdktrace.SamplingParameters) sdktrace.SamplingResult {
	return sdktrace.SamplingResult{Decision: sdktrace.Drop, Tracestate: trace.SpanContextFromContext(p.ParentContext).TraceState()}
}

// polled reports whether an API request reads: a GET or HEAD, or waitTasks,
// the long poll for task results.
func polled(method, path string) bool {
	return method == http.MethodGet || method == http.MethodHead || strings.HasSuffix(path, "/tasks/wait")
}

func (s rootSampler) Description() string {
	return "RootSampler{" + s.ratio.Description() + ", edge and API reads " + s.edge.Description() + ", no pass or gRPC roots}"
}
