package telemetry

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	commonpb "go.opentelemetry.io/proto/otlp/common/v1"
	resourcepb "go.opentelemetry.io/proto/otlp/resource/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

// HostTraceSocket is where a host's agent receives OTLP spans from its own
// tracer and from the snapshotter, which it sends to the server over its
// session. Only root reaches it.
const HostTraceSocket = "/run/lazycloud-traces.sock"

const (
	// maxHostTraceBytes and maxHostSpans bound one batch a host sends; an
	// exporter batch holds at most 512 spans.
	maxHostTraceBytes = 4 << 20
	maxHostSpans      = 4096
	// hostTraceQueue bounds the batches waiting to be forwarded. Past it
	// batches are dropped: spans only explain latency.
	hostTraceQueue = 64
	// hostTraceTimeout bounds one forward to the collector.
	hostTraceTimeout = 10 * time.Second
)

// hostServices are the service names a host's spans may carry; any other
// becomes lazycloud-agent, so a host never speaks as the control plane.
var hostServices = map[string]bool{"lazycloud-agent": true, "lazycloud-snapshotter": true} //nolint:gochecknoglobals // A constant set.

// HostTraces forwards the spans hosts send over their sessions to the
// server's collector. Hosts are not trusted: each batch is bounded, and its
// resources keep only their service name and version, stamped with the host
// that sent them.
type HostTraces struct {
	conn   *grpc.ClientConn
	client collector.TraceServiceClient
	queue  chan hostBatch
	log    *slog.Logger
}

type hostBatch struct {
	host string
	otlp []byte
}

// HostTraces returns the forwarder of hosts' spans, or nil when the binary
// exports no traces, which drops them.
func (t *Telemetry) HostTraces(logger *slog.Logger) (*HostTraces, error) {
	if t.cfg.OTLPEndpoint == "" {
		return nil, nil
	}
	creds := credentials.NewClientTLSFromCert(nil, "")
	if t.cfg.OTLPInsecure {
		creds = insecure.NewCredentials()
	}
	conn, err := grpc.NewClient(t.cfg.OTLPEndpoint, grpc.WithTransportCredentials(creds))
	if err != nil {
		return nil, fmt.Errorf("dial the trace collector: %w", err)
	}
	return &HostTraces{conn: conn, client: collector.NewTraceServiceClient(conn), queue: make(chan hostBatch, hostTraceQueue), log: logger}, nil
}

// Offer queues a batch host sent, or drops it when the queue is full. A nil
// forwarder drops everything.
func (h *HostTraces) Offer(host string, otlp []byte) {
	if h == nil || len(otlp) > maxHostTraceBytes {
		return
	}
	select {
	case h.queue <- hostBatch{host: host, otlp: otlp}:
	default:
	}
}

// Run forwards queued batches until ctx ends, then closes the connection.
func (h *HostTraces) Run(ctx context.Context) error {
	if h == nil {
		return nil
	}
	defer func() { _ = h.conn.Close() }()
	for {
		select {
		case <-ctx.Done():
			return nil
		case b := <-h.queue:
			request, ok := hostRequest(b)
			if !ok {
				continue
			}
			call, cancel := context.WithTimeout(ctx, hostTraceTimeout)
			_, err := h.client.Export(call, request)
			cancel()
			if err != nil && ctx.Err() == nil {
				h.log.DebugContext(ctx, "forwarding host spans failed", "host_id", b.host, "error", err)
			}
		}
	}
}

// hostRequest decodes a host's batch and rewrites its resources, or
// reports false for one that is malformed or too large.
func hostRequest(b hostBatch) (*collector.ExportTraceServiceRequest, bool) {
	var request collector.ExportTraceServiceRequest
	if err := proto.Unmarshal(b.otlp, &request); err != nil {
		return nil, false
	}
	spans := 0
	for _, rs := range request.GetResourceSpans() {
		for _, ss := range rs.GetScopeSpans() {
			spans += len(ss.GetSpans())
		}
		service, version := "lazycloud-agent", ""
		for _, a := range rs.GetResource().GetAttributes() {
			switch a.GetKey() {
			case "service.name":
				if name := a.GetValue().GetStringValue(); hostServices[name] {
					service = name
				}
			case "service.version":
				version = a.GetValue().GetStringValue()
			}
		}
		rs.Resource = &resourcepb.Resource{Attributes: []*commonpb.KeyValue{
			stringAttr("service.name", service), stringAttr("service.version", version), stringAttr("lazycloud."+KeyHost, b.host),
		}}
		rs.SchemaUrl = ""
	}
	return &request, spans <= maxHostSpans
}

func stringAttr(key, value string) *commonpb.KeyValue {
	return &commonpb.KeyValue{Key: key, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: value}}}
}
