package edge

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// A container connection is one HTTP exchange with a container over its
// host's data connection: to the supervisor's control API, or to a port the
// container listens on. An upgraded exchange (101) carries raw bytes both
// ways; its response Body is an io.ReadWriteCloser.

// ErrContainerUnreachable means nothing reached the container: its host has
// no data connection here, or the agent does not run it.
var ErrContainerUnreachable = errors.New("the container cannot be reached")

// Target is where in the container an exchange goes.
type Target struct {
	// Port is a port the container listens on; zero is the supervisor's
	// control API.
	Port int
}

// ControlAPI is the supervisor's control API.
var ControlAPI = Target{} //nolint:gochecknoglobals // a constant value

// RoundTrip sends req to the container on host and returns its response.
// The request body streams to the container and the response body streams
// back; closing the response body ends the exchange. An upgrade request
// sends no body: after a 101 the response body is the tunnel.
func (e *Edge) RoundTrip(ctx context.Context, host, container uuid.UUID, target Target, req *http.Request) (*http.Response, error) {
	p, err := e.stream(ctx, host)
	if err != nil {
		if errors.Is(err, errNoStream) {
			return nil, fmt.Errorf("%w: %w", ErrContainerUnreachable, err)
		}
		return nil, err
	}
	stop := context.AfterFunc(ctx, func() { p.finish(status.Error(codes.Canceled, "the caller left")) })
	fail := func(err error) (*http.Response, error) {
		stop()
		p.finish(nil)
		return nil, err
	}
	upgrade := strings.EqualFold(req.Header.Get("Connection"), "upgrade") || req.Header.Get("Upgrade") != ""
	head := &hostproto.HttpRequest{Method: req.Method, Uri: req.URL.RequestURI(), Host: req.Host}
	for name, values := range req.Header {
		if !upgrade || (name != "Connection" && name != "Upgrade") {
			if isHop(name) {
				continue
			}
		}
		for _, value := range values {
			head.Headers = append(head.Headers, &hostproto.Header{Name: name, Value: value})
		}
	}
	if req.ContentLength > 0 && !upgrade {
		head.Headers = append(head.Headers, &hostproto.Header{Name: "Content-Length", Value: fmt.Sprint(req.ContentLength)})
	}
	rh := &hostproto.RequestHead{ContainerId: container.String()}
	if target.Port == 0 {
		rh.Kind = &hostproto.RequestHead_Control{Control: head}
	} else {
		rh.Kind = &hostproto.RequestHead_Port{Port: &hostproto.PortRequest{Port: int32(target.Port), Http: head}} //nolint:gosec // Ports fit.
	}
	if err := p.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_Head{Head: rh}}); err != nil {
		return fail(fmt.Errorf("send request head: %w", err))
	}
	sending := make(chan error, 1)
	if upgrade || req.Body == nil || req.Body == http.NoBody {
		if !upgrade {
			if err := p.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_End{End: &hostproto.End{}}}); err != nil {
				return fail(fmt.Errorf("send end: %w", err))
			}
		}
		sending <- nil
	} else {
		go func() { //nolint:gocritic // the response body's Close waits for it
			sending <- sendRequestBody(p, &requestBody{stream: req.Body})
		}()
	}
	first, err := p.stream.Recv()
	if err != nil {
		stop()
		p.finish(nil)
		if p.relayed && status.Code(err) == codes.Unavailable {
			e.forgetPeer(host)
			return nil, fmt.Errorf("%w: %w", ErrContainerUnreachable, err)
		}
		return nil, fmt.Errorf("receive response head: %w", err)
	}
	if refusal := first.GetError(); refusal != nil {
		stop()
		p.finish(nil)
		if refusal.GetKind() == hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING {
			return nil, fmt.Errorf("%w: %s", ErrContainerUnreachable, refusal.GetMessage())
		}
		return nil, fmt.Errorf("the container failed the request: %s", refusal.GetMessage())
	}
	h := first.GetHead()
	if h == nil {
		return fail(errors.New("the agent answered without a response head"))
	}
	resp := &http.Response{
		StatusCode: int(h.GetStatus()), Status: fmt.Sprintf("%d %s", h.GetStatus(), http.StatusText(int(h.GetStatus()))),
		Proto: "HTTP/1.1", ProtoMajor: 1, ProtoMinor: 1, Header: http.Header{}, Request: req, ContentLength: -1,
	}
	for _, hh := range h.GetHeaders() {
		resp.Header.Add(hh.GetName(), hh.GetValue())
	}
	body := &containerBody{p: p, stop: stop, tunnel: resp.StatusCode == http.StatusSwitchingProtocols}
	resp.Body = body
	return resp, nil
}

// containerBody reads a response body, or both directions of a tunnel, off
// one agent stream.
type containerBody struct {
	p      *parkedStream
	stop   func() bool
	tunnel bool

	pending []byte
	done    bool

	writeMu sync.Mutex
	closed  sync.Once
}

func (b *containerBody) Read(out []byte) (int, error) {
	for len(b.pending) == 0 {
		if b.done {
			return 0, io.EOF
		}
		msg, err := b.p.stream.Recv()
		if err != nil {
			b.done = true
			if errors.Is(err, io.EOF) {
				return 0, io.ErrUnexpectedEOF
			}
			return 0, fmt.Errorf("receive body: %w", err)
		}
		switch body := msg.GetBody().(type) {
		case *hostproto.ForwardUp_Data:
			b.pending = body.Data
		case *hostproto.ForwardUp_End:
			b.done = true
		case *hostproto.ForwardUp_Error:
			b.done = true
			return 0, fmt.Errorf("the container failed the response: %s", body.Error.GetMessage())
		default:
			b.done = true
			return 0, errors.New("unexpected message in a response body")
		}
	}
	n := copy(out, b.pending)
	b.pending = b.pending[n:]
	return n, nil
}

// Write sends tunnel bytes to the container.
func (b *containerBody) Write(data []byte) (int, error) {
	if !b.tunnel {
		return 0, errors.New("only an upgraded exchange takes writes")
	}
	b.writeMu.Lock()
	defer b.writeMu.Unlock()
	for start := 0; start < len(data); start += bodyChunk {
		chunk := bytes.Clone(data[start:min(start+bodyChunk, len(data))])
		if err := b.p.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_Data{Data: chunk}}); err != nil {
			return start, fmt.Errorf("send tunnel bytes: %w", err)
		}
	}
	return len(data), nil
}

// CloseWrite tells the container the client is done sending.
func (b *containerBody) CloseWrite() error {
	b.writeMu.Lock()
	defer b.writeMu.Unlock()
	if err := b.p.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_End{End: &hostproto.End{}}}); err != nil {
		return fmt.Errorf("send end: %w", err)
	}
	return nil
}

// Close ends the exchange. The request body's sender then fails its next
// send, or ends when the caller's body does.
func (b *containerBody) Close() error {
	b.closed.Do(func() {
		b.stop()
		b.p.finish(nil)
	})
	return nil
}

func isHop(name string) bool {
	for _, hop := range hopHeaders {
		if strings.EqualFold(hop, name) {
			return true
		}
	}
	return strings.EqualFold(name, "Content-Length")
}
