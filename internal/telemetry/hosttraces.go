package telemetry

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

	"github.com/prometheus/client_golang/prometheus"
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
	// maxHostTraceBytes bounds one batch a host sends; an exporter batch
	// holds at most 512 spans.
	maxHostTraceBytes = 4 << 20
	// hostSpansPerSecond and hostSpanBurst bound the spans each host may
	// send. A start records a few dozen.
	hostSpansPerSecond = 200
	hostSpanBurst      = 2000
	// hostBucketIdle drops the budget of a host that sent nothing for that
	// long; a new one starts full.
	hostBucketIdle = 10 * time.Minute
	// hostTraceQueue bounds the batches waiting to be forwarded. Past it
	// batches are dropped, since spans only explain latency.
	hostTraceQueue = 64
	// hostTraceTimeout bounds one forward to the collector.
	hostTraceTimeout = 10 * time.Second
)

// hostServices are the service names a host's spans may carry; any other
// becomes lazycloud-agent, so a host never speaks as the control plane.
var hostServices = map[string]bool{"lazycloud-agent": true, "lazycloud-snapshotter": true} //nolint:gochecknoglobals // A constant set.

// HostTraces forwards the spans hosts send over their sessions to the
// server's collector. Hosts are not trusted. Each host has a span budget,
// and every resource and span names the host that sent it and nothing a
// backend reads as identity or AWS metadata.
type HostTraces struct {
	conn    *grpc.ClientConn
	client  collector.TraceServiceClient
	queue   chan *collector.ExportTraceServiceRequest
	dropped prometheus.Counter
	log     *slog.Logger

	mu      sync.Mutex
	buckets map[string]*spanBucket
}

// spanBucket is one host's span budget.
type spanBucket struct {
	tokens float64
	last   time.Time
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
	dropped := prometheus.NewCounter(prometheus.CounterOpts{
		Name: "lazycloud_host_spans_dropped_total",
		Help: "Spans hosts sent that the server dropped: over a host's budget, malformed or with the queue full.",
	})
	t.Registry.MustRegister(dropped)
	return &HostTraces{
		conn: conn, client: collector.NewTraceServiceClient(conn), queue: make(chan *collector.ExportTraceServiceRequest, hostTraceQueue),
		dropped: dropped, log: logger, buckets: map[string]*spanBucket{},
	}, nil
}

// Offer queues a batch host sent, or drops it when it is malformed, over
// the host's budget or the queue is full. A nil forwarder drops everything.
func (h *HostTraces) Offer(host string, otlp []byte) {
	if h == nil {
		return
	}
	request, spans, ok := hostRequest(host, otlp)
	if !ok {
		h.dropped.Add(float64(spans))
		return
	}
	if !h.take(host, spans, time.Now()) {
		h.dropped.Add(float64(spans))
		return
	}
	select {
	case h.queue <- request:
	default:
		h.dropped.Add(float64(spans))
	}
}

// take spends spans of host's budget, refilled at hostSpansPerSecond up to
// hostSpanBurst, and reports whether it held them.
func (h *HostTraces) take(host string, spans int, now time.Time) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	for id, b := range h.buckets {
		if now.Sub(b.last) > hostBucketIdle {
			delete(h.buckets, id)
		}
	}
	b := h.buckets[host]
	if b == nil {
		b = &spanBucket{tokens: hostSpanBurst, last: now}
		h.buckets[host] = b
	}
	b.tokens = min(hostSpanBurst, b.tokens+now.Sub(b.last).Seconds()*hostSpansPerSecond)
	b.last = now
	if float64(spans) > b.tokens {
		return false
	}
	b.tokens -= float64(spans)
	return true
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
		case request := <-h.queue:
			call, cancel := context.WithTimeout(ctx, hostTraceTimeout)
			_, err := h.client.Export(call, request)
			cancel()
			if err != nil && ctx.Err() == nil {
				h.log.DebugContext(ctx, "forwarding host spans failed", "error", err)
			}
		}
	}
}

// hostRequest decodes a batch host sent and rewrites it: each resource
// keeps its service name and version, each span loses the attributes a
// backend reads as identity or AWS metadata, and both name host. It returns
// the span count, and false for a batch that is malformed or too large.
func hostRequest(host string, otlp []byte) (*collector.ExportTraceServiceRequest, int, bool) {
	if len(otlp) > maxHostTraceBytes {
		return nil, 0, false
	}
	var request collector.ExportTraceServiceRequest
	if err := proto.Unmarshal(otlp, &request); err != nil {
		return nil, 0, false
	}
	hostAttr := stringAttr("lazycloud."+KeyHost, host)
	spans := 0
	for _, rs := range request.GetResourceSpans() {
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
			stringAttr("service.name", service), stringAttr("service.version", version), hostAttr,
		}}
		rs.SchemaUrl = ""
		for _, ss := range rs.GetScopeSpans() {
			for _, s := range ss.GetSpans() {
				spans++
				s.Attributes = append(hostSpanAttrs(s.GetAttributes()), hostAttr)
				for _, e := range s.GetEvents() {
					e.Attributes = hostSpanAttrs(e.GetAttributes())
				}
				if s.GetStatus() != nil {
					s.Status.Message = Redact(s.GetStatus().GetMessage())
				}
			}
		}
	}
	return &request, spans, true
}

// hostSpanAttrs drops the attributes a host may not set and removes URL
// queries from the rest.
func hostSpanAttrs(attrs []*commonpb.KeyValue) []*commonpb.KeyValue {
	out := attrs[:0]
	for _, a := range attrs {
		key := a.GetKey()
		if strings.HasPrefix(key, "aws.") || strings.HasPrefix(key, "service.") || key == "peer.service" || key == "lazycloud."+KeyHost {
			continue
		}
		if v, ok := a.GetValue().GetValue().(*commonpb.AnyValue_StringValue); ok {
			v.StringValue = Redact(v.StringValue)
		}
		out = append(out, a)
	}
	return out
}

func stringAttr(key, value string) *commonpb.KeyValue {
	return &commonpb.KeyValue{Key: key, Value: &commonpb.AnyValue{Value: &commonpb.AnyValue_StringValue{StringValue: value}}}
}
