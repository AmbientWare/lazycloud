package telemetry

import (
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

// rootSampler decides the traces a binary starts: ratio's share of them,
// except a gRPC call outside any traced step. Hosts call the server all the
// time (sessions, claim long polls, logs), so a call is recorded only as
// part of the trace of the step that made it.
type rootSampler struct{ ratio sdktrace.Sampler }

func (s rootSampler) ShouldSample(p sdktrace.SamplingParameters) sdktrace.SamplingResult {
	for _, a := range p.Attributes {
		if a.Key == "rpc.system.name" || a.Key == "rpc.system" {
			return sdktrace.SamplingResult{Decision: sdktrace.Drop, Tracestate: trace.SpanContextFromContext(p.ParentContext).TraceState()}
		}
	}
	return s.ratio.ShouldSample(p)
}

func (s rootSampler) Description() string {
	return "RootSampler{" + s.ratio.Description() + ", no gRPC roots}"
}
