package telemetry

import (
	"strings"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// PassPrefix starts the name of a scheduler pass's span. A pass never
// starts a trace: the steps it takes for a container record in that
// container's trace, and only the steps for none start their own.
const PassPrefix = "scheduler."

// rootSampler decides the traces a binary starts: edge's share of workload
// requests, which anyone may send, and ratio's share of the rest. It starts
// none for a scheduler pass or for a gRPC call outside a traced step: hosts
// call the server all the time, so a call records only in the trace of the
// step that made it.
type rootSampler struct{ ratio, edge sdktrace.Sampler }

func (s rootSampler) ShouldSample(p sdktrace.SamplingParameters) sdktrace.SamplingResult {
	drop := sdktrace.SamplingResult{Decision: sdktrace.Drop, Tracestate: trace.SpanContextFromContext(p.ParentContext).TraceState()}
	if strings.HasPrefix(p.Name, PassPrefix) {
		return drop
	}
	for _, a := range p.Attributes {
		if a.Key == "rpc.system.name" || a.Key == "rpc.system" {
			return drop
		}
	}
	if strings.HasPrefix(p.Name, "edge") {
		return s.edge.ShouldSample(p)
	}
	return s.ratio.ShouldSample(p)
}

func (s rootSampler) Description() string {
	return "RootSampler{" + s.ratio.Description() + ", edge " + s.edge.Description() + ", no pass or gRPC roots}"
}
