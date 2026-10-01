package agent

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net"
	"net/http"
	"strconv"
	"sync"
	"time"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/supervisor"
)

const (
	// minIdleStreams are kept open for the edge, so a request never waits
	// for a stream to open.
	minIdleStreams = 4
	// maxDataStreams bounds open Forward streams, idle and busy.
	maxDataStreams = 4096
	// maxStreamsPerEvent bounds one Listen request for more streams.
	maxStreamsPerEvent = 256
	// dataChunk is the largest body chunk in one message.
	dataChunk = 32 << 10
	// inboundQueue bounds edge messages read ahead of the request body's
	// reader; past it gRPC flow control holds the edge.
	inboundQueue = 4
	// topUpInterval restores idle streams that ended without a request.
	topUpInterval = 5 * time.Second
	// closeWait bounds how long a finished stream waits for the edge to end
	// it before cancelling.
	closeWait = 30 * time.Second
)

// dataLink keeps the agent's data connection to the edge: Forward streams
// opened in advance, which the edge assigns one request each. It is separate
// from the control session, so request bodies never delay commands.
type dataLink struct {
	a      *Agent
	client hostproto.HostDataClient

	mu    sync.Mutex
	idle  int
	total int
	// streams covers every goroutine of the link; run waits for them.
	streams sync.WaitGroup
}

// run keeps Listen open, reconnecting with backoff, and holds idle streams
// while it is open.
func (d *dataLink) run(ctx context.Context) {
	defer d.streams.Wait()
	delay := minSessionBackoff
	for ctx.Err() == nil {
		began := time.Now()
		err := d.listen(ctx)
		if ctx.Err() != nil {
			return
		}
		if time.Since(began) > time.Minute {
			delay = minSessionBackoff
		}
		d.a.log.Warn("data connection ended", "error", err, "retry_in", delay)
		if !sleep(ctx, delay/2+rand.N(delay/2+1)) { //nolint:gosec // jitter needs no cryptographic randomness
			return
		}
		delay = min(2*delay, maxSessionBackoff)
	}
}

func (d *dataLink) listen(ctx context.Context) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	events, err := d.client.Listen(ctx, &hostproto.ListenRequest{})
	if err != nil {
		return fmt.Errorf("listen: %w", err)
	}
	received := make(chan *hostproto.ListenEvent)
	failed := make(chan error, 1)
	d.streams.Go(func() {
		for {
			event, err := events.Recv()
			if err != nil {
				failed <- err
				return
			}
			select {
			case received <- event:
			case <-ctx.Done():
				return
			}
		}
	})
	d.topUp(ctx)
	ticker := time.NewTicker(topUpInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return fmt.Errorf("listen: %w", ctx.Err())
		case err := <-failed:
			return fmt.Errorf("receive: %w", err)
		case event := <-received:
			d.open(ctx, min(int(event.GetStreams()), maxStreamsPerEvent))
		case <-ticker.C:
			d.topUp(ctx)
		}
	}
}

// topUp opens streams until minIdleStreams are idle.
func (d *dataLink) topUp(ctx context.Context) {
	d.mu.Lock()
	missing := minIdleStreams - d.idle
	d.mu.Unlock()
	d.open(ctx, missing)
}

// open starts n idle streams, bounded by maxDataStreams.
func (d *dataLink) open(ctx context.Context, n int) {
	for range n {
		d.mu.Lock()
		if d.total >= maxDataStreams {
			d.mu.Unlock()
			return
		}
		d.total++
		d.idle++
		d.mu.Unlock()
		d.streams.Go(func() { d.forward(ctx) })
	}
}

type forwardStream = hostproto.HostData_ForwardClient

// forward opens one stream, waits idle for the edge to assign a request,
// replaces itself and serves the request. The stream then half-closes and
// waits for the edge to end it, so the end of the response is never lost to
// a cancel.
func (d *dataLink) forward(ctx context.Context) {
	assigned := false
	defer func() {
		d.mu.Lock()
		d.total--
		if !assigned {
			d.idle--
		}
		d.mu.Unlock()
	}()
	streamCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := d.client.Forward(streamCtx)
	if err != nil {
		return
	}
	first, err := stream.Recv()
	if err != nil {
		return
	}
	assigned = true
	d.mu.Lock()
	d.idle--
	refill := d.idle < minIdleStreams
	d.mu.Unlock()
	if refill {
		d.open(ctx, 1)
	}
	in := receive(stream, &d.streams)
	head := first.GetHead()
	switch kind := head.GetKind().(type) {
	case *hostproto.RequestHead_Http:
		d.serveHTTP(streamCtx, stream, in, d.a.lookup(head.GetContainerId()), kind.Http)
	case *hostproto.RequestHead_Sync:
		d.serveSync(stream, in, d.a.lookup(head.GetContainerId()))
	default:
		_ = stream.Send(forwardError(hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_FAILED, "the first message must be a request head"))
	}
	_ = stream.CloseSend()
	in.stop()
	timer := time.NewTimer(closeWait)
	defer timer.Stop()
	select {
	case <-in.ended:
	case <-timer.C:
		cancel()
		<-in.ended
	}
}

// serveHTTP forwards one request to the container's supervisor and streams
// the response back. A 101 response turns the stream into a byte tunnel.
func (d *dataLink) serveHTTP(ctx context.Context, stream forwardStream, in *inbound, c *container, head *hostproto.HttpRequest) {
	if c == nil || !c.servesHTTP() {
		_ = stream.Send(forwardError(hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING, "the container does not serve HTTP on this host"))
		return
	}
	req, err := http.NewRequestWithContext(ctx, head.GetMethod(), "http://container"+head.GetUri(), in)
	if err != nil {
		_ = stream.Send(forwardError(hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_FAILED, "invalid request: "+err.Error()))
		return
	}
	req.Host = head.GetHost()
	for _, h := range head.GetHeaders() {
		req.Header.Add(h.GetName(), h.GetValue())
	}
	req.ContentLength = -1
	if length := req.Header.Get("Content-Length"); length != "" {
		if n, err := strconv.ParseInt(length, 10, 64); err == nil && n >= 0 {
			req.ContentLength = n
		}
	}
	upgrade := req.Header.Get("Upgrade") != ""
	if req.ContentLength == 0 || upgrade {
		req.Body = http.NoBody
	}
	resp, err := c.requests.RoundTrip(req)
	if err != nil {
		kind := hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_FAILED
		var opErr *net.OpError
		if errors.As(err, &opErr) && opErr.Op == "dial" {
			// Nothing reached the workload.
			kind = hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING
		}
		_ = stream.Send(forwardError(kind, "forward to the container: "+err.Error()))
		return
	}
	defer func() { _ = resp.Body.Close() }()
	if reason := resp.Header.Get(supervisor.BusyHeader); resp.StatusCode == http.StatusServiceUnavailable && reason != "" {
		_ = stream.Send(forwardError(hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_BUSY, reason))
		return
	}
	headers := make([]*hostproto.Header, 0, len(resp.Header))
	for name, values := range resp.Header {
		for _, value := range values {
			headers = append(headers, &hostproto.Header{Name: name, Value: value})
		}
	}
	if err := stream.Send(&hostproto.ForwardUp{Body: &hostproto.ForwardUp_Head{Head: &hostproto.ResponseHead{
		Status: int32(resp.StatusCode), Headers: headers, //nolint:gosec // HTTP status codes fit
	}}}); err != nil {
		return
	}
	var tunnel sync.WaitGroup
	if conn, ok := resp.Body.(io.ReadWriteCloser); ok && resp.StatusCode == http.StatusSwitchingProtocols {
		// The client's bytes go to the upgraded connection; when the client
		// closes, so does the connection, which ends the copy below.
		tunnel.Go(func() {
			_, _ = io.Copy(conn, in)
			_ = conn.Close()
		})
	}
	err = sendBody(stream, resp.Body)
	in.stop()
	tunnel.Wait()
	if err == nil {
		_ = stream.Send(&hostproto.ForwardUp{Body: &hostproto.ForwardUp_End{End: &hostproto.End{}}})
	}
}

// sendBody streams r up in chunks as they arrive.
func sendBody(stream forwardStream, r io.Reader) error {
	buf := make([]byte, dataChunk)
	for {
		n, err := r.Read(buf)
		if n > 0 {
			chunk := make([]byte, n)
			copy(chunk, buf[:n])
			if sendErr := stream.Send(&hostproto.ForwardUp{Body: &hostproto.ForwardUp_Data{Data: chunk}}); sendErr != nil {
				return fmt.Errorf("send body: %w", sendErr)
			}
		}
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("read body: %w", err)
		}
	}
}

func forwardError(kind hostproto.ForwardErrorKind, message string) *hostproto.ForwardUp {
	return &hostproto.ForwardUp{Body: &hostproto.ForwardUp_Error{Error: &hostproto.ForwardError{Kind: kind, Message: message}}}
}

// inbound reads the edge's messages after the head on one goroutine, so the
// request body, a tunnel and the final wait for the stream's end never
// receive concurrently. Read returns the body bytes until End.
type inbound struct {
	messages chan *hostproto.ForwardDown
	// failed holds the receive error that ended the messages early.
	failed  error
	stopped chan struct{}
	stopper sync.Once
	// ended is closed once the edge ended the stream.
	ended chan struct{}

	pending []byte
	done    bool
}

func receive(stream forwardStream, owner *sync.WaitGroup) *inbound {
	in := &inbound{
		messages: make(chan *hostproto.ForwardDown, inboundQueue),
		stopped:  make(chan struct{}),
		ended:    make(chan struct{}),
	}
	owner.Go(func() { in.run(stream) })
	return in
}

func (in *inbound) run(stream forwardStream) {
	defer close(in.ended)
	defer close(in.messages)
	for {
		msg, err := stream.Recv()
		if err != nil {
			if !errors.Is(err, io.EOF) {
				in.failed = err
			}
			return
		}
		select {
		case in.messages <- msg:
		case <-in.stopped:
			// Nobody reads any more; keep receiving so the edge is never
			// held by flow control while it finishes.
		}
	}
}

// stop discards what the edge still sends.
func (in *inbound) stop() {
	in.stopper.Do(func() { close(in.stopped) })
}

// Read returns body bytes until End, and an error if the stream ended
// first. Only one goroutine reads.
func (in *inbound) Read(p []byte) (int, error) {
	for len(in.pending) == 0 {
		if in.done {
			return 0, io.EOF
		}
		var msg *hostproto.ForwardDown
		var ok bool
		select {
		case msg, ok = <-in.messages:
		case <-in.stopped:
			return 0, io.EOF
		}
		if !ok {
			if in.failed != nil {
				return 0, fmt.Errorf("receive body: %w", in.failed)
			}
			return 0, io.ErrUnexpectedEOF
		}
		switch body := msg.GetBody().(type) {
		case *hostproto.ForwardDown_Data:
			in.pending = body.Data
		case *hostproto.ForwardDown_End:
			in.done = true
		default:
			return 0, errors.New("unexpected message in a request body")
		}
	}
	n := copy(p, in.pending)
	in.pending = in.pending[n:]
	return n, nil
}

func (in *inbound) Close() error { return nil }
