package edge

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// streamWait bounds how long a request waits for an agent to open a stream
// once it asked for one.
const streamWait = 10 * time.Second

// maxIdleStreams bounds the idle streams one host parks; more are ended at
// once and the agent opens them again only when asked.
const maxIdleStreams = 256

// hostStreams holds each connected agent's idle Forward streams.
type hostStreams struct {
	mu     sync.Mutex
	hosts  map[uuid.UUID]*hostPool
	closed bool
	// shut is closed by Shutdown, which ends every Listen call.
	shut chan struct{}
}

type hostPool struct {
	idle []*parkedStream
	// waiting counts requests that found no idle stream.
	waiting int
	// opened is closed and replaced when a stream parks.
	opened chan struct{}
	// demand reaches the host's Listen call, if one is open.
	demand chan int32
}

// parkedStream is an agent's Forward stream waiting for a request.
type parkedStream struct {
	stream grpc.BidiStreamingServer[hostproto.ForwardUp, hostproto.ForwardDown]
	// done ends the RPC with err once the request is over.
	done chan struct{}
	err  error
	once sync.Once
}

func (p *parkedStream) finish(err error) {
	p.once.Do(func() {
		p.err = err
		close(p.done)
	})
}

func (h *hostStreams) poolLocked(host uuid.UUID) *hostPool {
	p := h.hosts[host]
	if p == nil {
		p = &hostPool{opened: make(chan struct{})}
		h.hosts[host] = p
	}
	return p
}

// Server implements hostproto.HostDataServer on the host connection's gRPC
// server, whose interceptors authenticate the agent.
type Server struct {
	hostproto.UnimplementedHostDataServer
	edge   *Edge
	hostOf func(context.Context) (compute.HostID, bool)
}

// DataServer returns the HostData service for agents. hostOf names the host
// the connection's interceptors authenticated.
func (e *Edge) DataServer(hostOf func(context.Context) (compute.HostID, bool)) *Server {
	return &Server{edge: e, hostOf: hostOf}
}

func (s *Server) host(ctx context.Context) (uuid.UUID, error) {
	h, ok := s.hostOf(ctx)
	if !ok {
		return uuid.Nil, status.Error(codes.Unauthenticated, "a host token is required")
	}
	return uuid.UUID(h), nil
}

// Listen tells the agent when the edge needs more idle streams.
func (s *Server) Listen(_ *hostproto.ListenRequest, stream grpc.ServerStreamingServer[hostproto.ListenEvent]) error {
	id, err := s.host(stream.Context())
	if err != nil {
		return err
	}
	h := &s.edge.hosts
	demand := make(chan int32, 1)
	h.mu.Lock()
	pool := h.poolLocked(id)
	pool.demand = demand
	h.mu.Unlock()
	defer func() {
		h.mu.Lock()
		if pool.demand == demand {
			pool.demand = nil
		}
		h.mu.Unlock()
	}()
	for {
		select {
		case <-stream.Context().Done():
			return nil
		case <-h.shut:
			return status.Error(codes.Unavailable, "the server is shutting down")
		case n := <-demand:
			if err := stream.Send(&hostproto.ListenEvent{Streams: n}); err != nil {
				return fmt.Errorf("send demand: %w", err)
			}
		}
	}
}

// Forward parks the agent's stream until a request takes it, then ends the
// RPC when the request is over.
func (s *Server) Forward(stream grpc.BidiStreamingServer[hostproto.ForwardUp, hostproto.ForwardDown]) error {
	id, err := s.host(stream.Context())
	if err != nil {
		return err
	}
	h := &s.edge.hosts
	p := &parkedStream{stream: stream, done: make(chan struct{})}
	h.mu.Lock()
	if h.closed {
		h.mu.Unlock()
		return status.Error(codes.Unavailable, "the server is shutting down")
	}
	pool := h.poolLocked(id)
	if len(pool.idle) >= maxIdleStreams {
		h.mu.Unlock()
		return nil
	}
	pool.idle = append(pool.idle, p)
	close(pool.opened)
	pool.opened = make(chan struct{})
	h.mu.Unlock()
	select {
	case <-p.done:
		return p.err
	case <-stream.Context().Done():
	}
	h.mu.Lock()
	for n, idle := range pool.idle {
		if idle == p {
			pool.idle = append(pool.idle[:n], pool.idle[n+1:]...)
			h.mu.Unlock()
			return nil
		}
	}
	h.mu.Unlock()
	// A request took the stream as the agent went away; it ends the RPC.
	<-p.done
	return p.err
}

// Shutdown ends every idle stream and refuses new ones, so a graceful stop
// of the gRPC server only waits for requests in flight.
func (e *Edge) Shutdown() {
	h := &e.hosts
	h.mu.Lock()
	if !h.closed {
		close(h.shut)
	}
	h.closed = true
	var idle []*parkedStream
	for _, pool := range h.hosts {
		idle = append(idle, pool.idle...)
		pool.idle = nil
	}
	h.mu.Unlock()
	for _, p := range idle {
		p.finish(status.Error(codes.Unavailable, "the server is shutting down"))
	}
}

// errNoStream means the container's agent opened no stream in time.
var errNoStream = errors.New("the container's host is not connected")

// take returns an idle stream of host, asking the agent for more when none
// is idle, until ctx ends or streamWait passes.
func (h *hostStreams) take(ctx context.Context, host uuid.UUID) (*parkedStream, error) {
	timer := time.NewTimer(streamWait)
	defer timer.Stop()
	asked := false
	for {
		h.mu.Lock()
		pool := h.poolLocked(host)
		if n := len(pool.idle); n > 0 {
			// The newest stream is the one most likely still connected.
			p := pool.idle[n-1]
			pool.idle = pool.idle[:n-1]
			if asked {
				pool.waiting--
				asked = false
			}
			h.mu.Unlock()
			if p.stream.Context().Err() != nil {
				p.finish(nil)
				continue
			}
			return p, nil
		}
		if !asked {
			pool.waiting++
			asked = true
		}
		opened := pool.opened
		if pool.demand != nil {
			// The agent opens streams until this many are idle, so a
			// repeated ask never opens more.
			select {
			case pool.demand <- int32(min(pool.waiting, maxIdleStreams)): //nolint:gosec // bounded by maxIdleStreams
			default:
			}
		}
		h.mu.Unlock()
		select {
		case <-opened:
		case <-timer.C:
			h.release(host)
			return nil, errNoStream
		case <-ctx.Done():
			h.release(host)
			return nil, fmt.Errorf("wait for a stream: %w", ctx.Err())
		}
	}
}

func (h *hostStreams) release(host uuid.UUID) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if pool := h.hosts[host]; pool != nil && pool.waiting > 0 {
		pool.waiting--
	}
}
