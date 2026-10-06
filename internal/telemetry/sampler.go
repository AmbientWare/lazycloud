package telemetry

import (
	"net/http"
	"strings"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.43.0"
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
	return s.pick(p).ShouldSample(p)
}

func (s rootSampler) pick(p sdktrace.SamplingParameters) sdktrace.Sampler {
	switch {
	case strings.HasPrefix(p.Name, PassPrefix):
		return sdktrace.NeverSample()
	case strings.HasPrefix(p.Name, "edge"):
		return s.edge
	}
	for _, a := range p.Attributes {
		switch v := a.Value.AsString(); a.Key {
		case semconv.RPCSystemNameKey:
			return sdktrace.NeverSample()
		case semconv.HTTPRequestMethodKey:
			if v == http.MethodGet || v == http.MethodHead {
				return s.edge
			}
		case semconv.URLPathKey:
			// waitTasks, the long poll for task results.
			if strings.HasSuffix(v, "/tasks/wait") {
				return s.edge
			}
		}
	}
	return s.ratio
}

func (s rootSampler) Description() string {
	return "RootSampler{" + s.ratio.Description() + ", edge and API reads " + s.edge.Description() + ", no pass or gRPC roots}"
}
