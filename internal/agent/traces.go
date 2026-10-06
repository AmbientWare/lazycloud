package agent

import (
	"context"
	"errors"
	"net"
	"os"

	collector "go.opentelemetry.io/proto/otlp/collector/trace/v1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// maxTraceBatchBytes bounds one batch the receiver takes, as the server
// bounds what it forwards.
const maxTraceBatchBytes = 4 << 20

// traceReceiver takes OTLP spans from the agent's own exporter and the
// snapshotter's, and sends each batch to the server over the session while
// its queue has room. Batches that arrive with no session open are dropped,
// since spans only explain latency.
type traceReceiver struct {
	collector.UnimplementedTraceServiceServer
	a *Agent
}

func (r traceReceiver) Export(_ context.Context, request *collector.ExportTraceServiceRequest) (*collector.ExportTraceServiceResponse, error) {
	raw, err := proto.Marshal(request)
	if err == nil {
		r.a.reportIfRoom(&hostproto.HostMessage{Body: &hostproto.HostMessage_Traces{Traces: &hostproto.Traces{Otlp: raw}}})
	}
	return &collector.ExportTraceServiceResponse{}, nil
}

// serveTraces serves the receiver on cfg.TraceSocket until ctx ends. A
// socket that cannot be served is logged; the host runs without traces.
func (a *Agent) serveTraces(ctx context.Context) {
	socket := a.cfg.TraceSocket
	if err := os.Remove(socket); err != nil && !errors.Is(err, os.ErrNotExist) {
		a.log.Warn("removing a stale trace socket failed", "socket", socket, "error", err)
		return
	}
	listener, err := (&net.ListenConfig{}).Listen(ctx, "unix", socket)
	if err != nil {
		a.log.Warn("the host's spans will not reach the server", "socket", socket, "error", err)
		return
	}
	if err := os.Chmod(socket, 0o600); err != nil {
		_ = listener.Close()
		a.log.Warn("restricting the trace socket failed", "socket", socket, "error", err)
		return
	}
	server := grpc.NewServer(grpc.MaxRecvMsgSize(maxTraceBatchBytes))
	collector.RegisterTraceServiceServer(server, traceReceiver{a: a})
	stop := context.AfterFunc(ctx, server.Stop)
	defer stop()
	if err := server.Serve(listener); err != nil && ctx.Err() == nil {
		a.log.Warn("serving the trace socket failed", "socket", socket, "error", err)
	}
}
