package agent

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"

	"google.golang.org/grpc"
	"google.golang.org/grpc/mem"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// traceService takes OTLP span batches from the agent's own exporter and
// the snapshotter's, and sends each to the server over the session while
// its queue has room. A batch travels as the bytes that arrived: the server
// decodes, bounds and rewrites every batch a host sends. Batches that
// arrive with no session open are dropped, since spans only explain
// latency.
func traceService() *grpc.ServiceDesc {
	return &grpc.ServiceDesc{
		ServiceName: "opentelemetry.proto.collector.trace.v1.TraceService",
		HandlerType: (*any)(nil),
		Methods: []grpc.MethodDesc{{
			MethodName: "Export",
			Handler: func(srv any, _ context.Context, decode func(any) error, _ grpc.UnaryServerInterceptor) (any, error) {
				var batch []byte
				if err := decode(&batch); err != nil {
					return nil, err
				}
				if a, ok := srv.(*Agent); ok {
					a.reportIfRoom(&hostproto.HostMessage{Body: &hostproto.HostMessage_Traces{Traces: &hostproto.Traces{Otlp: batch}}})
				}
				// The empty bytes are an empty ExportTraceServiceResponse.
				return []byte(nil), nil
			},
		}},
	}
}

// rawCodec passes messages through as their encoded bytes.
type rawCodec struct{}

func (rawCodec) Marshal(v any) (mem.BufferSlice, error) {
	b, ok := v.([]byte)
	if !ok {
		return nil, fmt.Errorf("trace socket: cannot send %T", v)
	}
	return mem.BufferSlice{mem.SliceBuffer(b)}, nil
}

func (rawCodec) Unmarshal(data mem.BufferSlice, v any) error {
	b, ok := v.(*[]byte)
	if !ok {
		return fmt.Errorf("trace socket: cannot decode into %T", v)
	}
	*b = data.Materialize()
	return nil
}

func (rawCodec) Name() string { return "proto" }

// serveTraces serves traceService on cfg.TraceSocket until ctx ends. A
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
	server := grpc.NewServer(grpc.MaxRecvMsgSize(telemetry.MaxHostTraceBytes), grpc.ForceServerCodecV2(rawCodec{}))
	server.RegisterService(traceService(), a)
	stop := context.AfterFunc(ctx, server.Stop)
	defer stop()
	if err := server.Serve(listener); err != nil && ctx.Err() == nil {
		a.log.Warn("serving the trace socket failed", "socket", socket, "error", err)
	}
}
