package edge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync/atomic"
	"time"

	"github.com/AmbientWare/lazycloud/internal/billing"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

const (
	// replayBytes is the largest streamed body kept to send again when a
	// container refuses a request before its workload saw it.
	replayBytes = 64 << 10
	// maxForwardTries bounds containers one request is offered to.
	maxForwardTries = 16
	// bodyChunk is the largest body piece in one message.
	bodyChunk = 32 << 10
)

// Hop-by-hop headers describe one connection, and Proxy-Authorization is a
// credential no workload may see.
var hopHeaders = []string{ //nolint:gochecknoglobals // a constant list
	"Connection", "Keep-Alive", "Proxy-Authenticate", "Proxy-Authorization",
	"Proxy-Connection", "Te", "Trailer", "Transfer-Encoding", "Upgrade",
}

// ServeHTTP serves workload traffic: it resolves the host, authenticates,
// admits the request and forwards it to a container, or runs a function.
func (e *Edge) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	t, err := e.resolve(ctx, r.Host)
	if err != nil {
		e.fail(w, r, err)
		return
	}
	if t.authorized {
		if err := e.authorize(ctx, r, t.workload); err != nil {
			e.fail(w, r, err)
			return
		}
	}
	e.serveTarget(w, r, t, t.authorized)
}

// serveTarget serves a resolved request whose caller passed t's token
// policy; authorized says a platform credential was consumed and must not
// reach the workload.
func (e *Edge) serveTarget(w http.ResponseWriter, r *http.Request, t target, authorized bool) {
	if !t.workload.accepting {
		writeError(w, http.StatusNotFound, "the deployment is stopped")
		return
	}
	switch t.workload.kind {
	case apitypes.WorkloadKindFunction:
		e.invoke(w, r, t)
	case apitypes.WorkloadKindEndpoint:
		if r.URL.Path != t.release.route {
			writeError(w, http.StatusNotFound, "endpoint route not found")
			return
		}
		if !t.release.allows(r.Method) {
			w.Header().Set("Allow", strings.Join(t.release.methods, ", "))
			writeError(w, http.StatusMethodNotAllowed, "endpoint method not allowed")
			return
		}
		e.serveRecorded(w, r, t, func(w http.ResponseWriter, r *http.Request, rec *requestRecord) { e.proxy(w, r, t, authorized, rec) })
	case apitypes.WorkloadKindAsgi:
		e.serveRecorded(w, r, t, func(w http.ResponseWriter, r *http.Request, rec *requestRecord) { e.proxy(w, r, t, authorized, rec) })
	default:
		writeError(w, http.StatusNotFound, "no workload answers on this host")
	}
}

// fail maps an error to a response.
func (e *Edge) fail(w http.ResponseWriter, r *http.Request, err error) {
	var failed *releaseFailedError
	var payment *billing.PaymentRequiredError
	var limit *billing.LimitError
	switch {
	// Billing's refusals, with the reference's statuses: the account must
	// pay, or is at a plan limit it can act on.
	case errors.As(err, &payment):
		writeError(w, http.StatusPaymentRequired, payment.Error())
	case errors.As(err, &limit):
		writeError(w, http.StatusConflict, limit.Error())
	case errors.Is(err, errNoRoute):
		writeError(w, http.StatusNotFound, err.Error())
	case errors.Is(err, identity.ErrUnauthenticated):
		w.Header().Set("WWW-Authenticate", "Bearer")
		writeError(w, http.StatusUnauthorized, "a valid bearer token is required")
	case errors.Is(err, identity.ErrForbidden), errors.Is(err, identity.ErrNotFound):
		writeError(w, http.StatusForbidden, "the token cannot reach this workspace")
	case errors.Is(err, errTooManyWait):
		writeError(w, http.StatusTooManyRequests, "endpoint request buffer is full")
	case errors.Is(err, errNoCapacity):
		writeError(w, http.StatusGatewayTimeout, err.Error())
	case errors.As(err, &failed):
		writeError(w, http.StatusInternalServerError, failed.Error())
	case errors.Is(err, errBodyTooLarge):
		writeError(w, http.StatusRequestEntityTooLarge, err.Error())
	case errors.Is(err, errBusy), errors.Is(err, errNoStream), errors.Is(err, errBufferFull):
		writeError(w, http.StatusServiceUnavailable, err.Error())
	case r.Context().Err() != nil:
		// The client is gone.
	default:
		e.logger.ErrorContext(r.Context(), "edge request failed", "host", r.Host, "path", r.URL.Path, "error", err)
		writeError(w, http.StatusBadGateway, "the request could not reach the workload")
	}
}

func writeError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": message}) //nolint:errchkjson // The client is gone if this fails.
}

var (
	errBodyTooLarge = fmt.Errorf("the request body exceeds %d bytes", execution.MaxPayloadBytes)
	errBusy         = errors.New("every container is busy")
	errBufferFull   = errors.New("the edge holds too many request bodies; try again")
)

// isPlatformToken reports whether an Authorization value carries a
// LazyCloud credential, in any scheme.
func isPlatformToken(value string) bool {
	_, credential, found := strings.Cut(strings.TrimSpace(value), " ")
	if !found {
		credential = value
	}
	return strings.HasPrefix(strings.TrimSpace(credential), identity.TokenPrefix)
}

// requestBody is what the edge sends a container: the whole body when it
// is small or the workload is an endpoint, so it can be sent again, or the
// client's stream.
type requestBody struct {
	buffered []byte
	stream   io.Reader
	// upgrade sends no body and no end: after the 101 the client's bytes
	// follow, and their end closes the tunnel.
	upgrade bool
	// release returns the buffered bytes to the edge's budget.
	release func()
}

func (b *requestBody) replayable() bool { return b.stream == nil }

// maxBufferedBodyBytes bounds the request bodies one edge holds in memory
// at once; past it requests are refused as busy.
const maxBufferedBodyBytes = 512 << 20

// bodyBudget counts the buffered request bytes the edge holds.
type bodyBudget struct{ used atomic.Int64 }

func (b *bodyBudget) reserve(n int64) bool {
	if b.used.Add(n) > maxBufferedBodyBytes {
		b.used.Add(-n)
		return false
	}
	return true
}

// budgetReader reserves each chunk it reads.
type budgetReader struct {
	r        io.Reader
	budget   *bodyBudget
	reserved int64
}

func (br *budgetReader) Read(p []byte) (int, error) {
	n, err := br.r.Read(p)
	if n > 0 {
		if !br.budget.reserve(int64(n)) {
			return 0, errBufferFull
		}
		br.reserved += int64(n)
	}
	return n, err //nolint:wrapcheck // a body reader's EOF must pass through unwrapped
}

func (e *Edge) readBody(r *http.Request, endpoint bool) (*requestBody, error) {
	limit := int64(replayBytes)
	if endpoint {
		limit = execution.MaxPayloadBytes
	}
	if r.Body == nil || r.Body == http.NoBody || r.ContentLength == 0 {
		return &requestBody{release: func() {}}, nil
	}
	if endpoint || (r.ContentLength > 0 && r.ContentLength <= limit) {
		reader := &budgetReader{r: r.Body, budget: &e.bodies}
		release := func() { e.bodies.used.Add(-reader.reserved) }
		data, err := io.ReadAll(io.LimitReader(reader, limit+1))
		if err != nil {
			release()
			if errors.Is(err, errBufferFull) {
				return nil, errBufferFull
			}
			return nil, fmt.Errorf("read request body: %w", err)
		}
		if int64(len(data)) > limit {
			release()
			return nil, errBodyTooLarge
		}
		return &requestBody{buffered: data, release: release}, nil
	}
	return &requestBody{stream: r.Body, release: func() {}}, nil
}

// proxy forwards the request to a container of the target. A container that
// refuses before its workload saw the request is replaced by another while
// the body can be sent again; an endpoint's failed response is retried under
// its retry policy.
func (e *Edge) proxy(w http.ResponseWriter, r *http.Request, t target, authorized bool, rec *requestRecord) {
	ctx := r.Context()
	// Past max_pending the request is refused before its body is read.
	if e.overPending(t.release) {
		e.fail(w, r, errTooManyWait)
		return
	}
	upgrade := r.Header.Get("Upgrade") != ""
	body, err := e.readBody(r, t.workload.kind == apitypes.WorkloadKindEndpoint)
	if err != nil {
		e.fail(w, r, err)
		return
	}
	defer body.release()
	body.upgrade = upgrade
	rc := http.NewResponseController(w)
	// A streamed body is still being sent while the response comes back.
	_ = rc.EnableFullDuplex()
	head := e.requestHead(r, authorized, upgrade, body, rec.id)
	deadline := time.Now().Add(t.release.timeout)
	attempts := 1
	if t.workload.kind == apitypes.WorkloadKindEndpoint {
		attempts = t.release.attempts
	}
	policy := execution.RetryPolicyOf(t.release.spec)
	for attempt, tries := 1, 0; ; tries++ {
		lease, err := e.acquire(ctx, t, deadline)
		if err != nil {
			e.fail(w, r, err)
			return
		}
		// Ending is idempotent; this covers a response that breaks off and
		// aborts the handler.
		defer lease.end()
		rec.container = lease.slot.id
		ex, err := e.exchange(ctx, lease, rc, head, body, deadline)
		if err != nil {
			lease.end()
			e.fail(w, r, err)
			return
		}
		if refusal := ex.first.GetError(); refusal != nil {
			refused := refusal.GetKind() == hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_BUSY ||
				refusal.GetKind() == hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING
			if refused {
				lease.refused(refusal.GetKind() == hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING)
			} else {
				lease.end()
			}
			ex.close()
			if refused && body.replayable() && tries < maxForwardTries {
				continue
			}
			if refused {
				e.fail(w, r, errBusy)
				return
			}
			e.logger.WarnContext(ctx, "forward failed", "container_id", lease.slot.id, "message", refusal.GetMessage())
			writeError(w, http.StatusBadGateway, "the workload's container failed: "+refusal.GetMessage())
			return
		}
		status := int(ex.first.GetHead().GetStatus())
		if (status < 200 || status >= 400) && status != http.StatusSwitchingProtocols && attempt < attempts && body.replayable() {
			lease.end()
			ex.close()
			attempt++
			if !sleepUntil(ctx, policy.NextAttemptDelay(attempt), deadline) {
				e.fail(w, r, errNoCapacity)
				return
			}
			continue
		}
		e.respond(w, r, ex)
		lease.end()
		return
	}
}

// requestHead is the request as the container receives it: hop-by-hop
// headers and the platform's bearer token removed, X-Forwarded-* set.
func (e *Edge) requestHead(r *http.Request, authorized, upgrade bool, body *requestBody, request uuid.UUID) *hostproto.HttpRequest {
	header := r.Header.Clone()
	// The workload sees the platform's id for the request, never one the
	// caller made up.
	header.Set(RequestIDHeader, request.String())
	for _, name := range hopHeaders {
		header.Del(name)
	}
	if upgrade {
		header.Set("Connection", "Upgrade")
		header.Set("Upgrade", r.Header.Get("Upgrade"))
	}
	if authorized {
		// The token reached the platform; the workload never sees it.
		header.Del("Authorization")
	} else {
		// A public workload keeps Authorization values of its own, never a
		// platform credential.
		var own []string
		for _, value := range header.Values("Authorization") {
			if !isPlatformToken(value) {
				own = append(own, value)
			}
		}
		header.Del("Authorization")
		for _, value := range own {
			header.Add("Authorization", value)
		}
	}
	if client, _, err := net.SplitHostPort(r.RemoteAddr); err == nil {
		if prior := header.Get("X-Forwarded-For"); prior != "" {
			client = prior + ", " + client
		}
		header.Set("X-Forwarded-For", client)
	}
	header.Set("X-Forwarded-Host", r.Host)
	header.Set("X-Forwarded-Proto", e.urls.scheme)
	header.Del("Content-Length")
	if body.replayable() && len(body.buffered) > 0 {
		header.Set("Content-Length", fmt.Sprint(len(body.buffered)))
	} else if !body.replayable() && r.ContentLength > 0 {
		header.Set("Content-Length", fmt.Sprint(r.ContentLength))
	}
	out := &hostproto.HttpRequest{Method: r.Method, Uri: r.URL.RequestURI(), Host: r.Host}
	for name, values := range header {
		for _, value := range values {
			out.Headers = append(out.Headers, &hostproto.Header{Name: name, Value: value})
		}
	}
	return out
}

// exchangeState is one request on one agent stream.
type exchangeState struct {
	stream *parkedStream
	body   *requestBody
	// rc reaches the client's connection, to end a stalled body read.
	rc *http.ResponseController
	// first is the agent's first answer: the response head or an error.
	first *hostproto.ForwardUp
	// sending ends when the request body is sent; started says whether its
	// sender runs.
	sending chan struct{}
	started bool
	stop    func() bool
}

// exchange sends the head and body to the lease's container and waits for
// the first answer until deadline.
func (e *Edge) exchange(ctx context.Context, l *lease, rc *http.ResponseController, head *hostproto.HttpRequest, body *requestBody, deadline time.Time) (*exchangeState, error) {
	p, err := e.stream(ctx, l.slot.host)
	if err != nil {
		return nil, err
	}
	ex := &exchangeState{stream: p, body: body, rc: rc, sending: make(chan struct{})}
	// A client that leaves ends the stream, which cancels the request in
	// the container.
	ex.stop = context.AfterFunc(ctx, func() { p.finish(status.Error(codes.Canceled, "the client left")) })
	if err := p.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_Head{Head: &hostproto.RequestHead{
		ContainerId: l.slot.id.String(), Kind: &hostproto.RequestHead_Http{Http: head},
	}}}); err != nil {
		ex.close()
		return nil, fmt.Errorf("send request head: %w", err)
	}
	ex.started = true
	go func() { //nolint:gocritic // ex.close waits on sending
		defer close(ex.sending)
		_ = sendRequestBody(p, body)
	}()
	timer := time.AfterFunc(time.Until(deadline), func() { p.finish(status.Error(codes.DeadlineExceeded, "no response before the timeout")) })
	first, err := p.stream.Recv()
	timer.Stop()
	if err != nil {
		ex.close()
		if time.Now().After(deadline) {
			return nil, errNoCapacity
		}
		if p.relayed && status.Code(err) == codes.Unavailable {
			// The other edge no longer holds the host: the request never
			// reached the workload, so it is offered again, and the next
			// stream reads the host's link afresh.
			e.forgetPeer(l.slot.host)
			ex.first = forwardRefusal(hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING, err.Error())
			return ex, nil
		}
		return nil, fmt.Errorf("receive response head: %w", err)
	}
	ex.first = first
	return ex, nil
}

func forwardRefusal(kind hostproto.ForwardErrorKind, message string) *hostproto.ForwardUp {
	return &hostproto.ForwardUp{Body: &hostproto.ForwardUp_Error{Error: &hostproto.ForwardError{Kind: kind, Message: message}}}
}

// close ends the RPC, which stops the body sender, and waits for it. A
// sender still reading a streamed body may be blocked on a client that
// stopped sending; a read deadline in the past ends that read.
func (ex *exchangeState) close() {
	ex.stop()
	ex.stream.finish(nil)
	if !ex.started {
		return
	}
	select {
	case <-ex.sending:
		return
	default:
	}
	if !ex.body.replayable() && !ex.body.upgrade && ex.rc != nil {
		_ = ex.rc.SetReadDeadline(time.Now())
	}
	<-ex.sending
}

func sendRequestBody(p *parkedStream, body *requestBody) error {
	if body.upgrade {
		return nil
	}
	send := func(data []byte) error {
		return p.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_Data{Data: data}})
	}
	if body.replayable() {
		for start := 0; start < len(body.buffered); start += bodyChunk {
			if err := send(body.buffered[start:min(start+bodyChunk, len(body.buffered))]); err != nil {
				return fmt.Errorf("send body: %w", err)
			}
		}
	} else {
		buf := make([]byte, bodyChunk)
		for {
			n, err := body.stream.Read(buf)
			if n > 0 {
				chunk := make([]byte, n)
				copy(chunk, buf[:n])
				if serr := send(chunk); serr != nil {
					return fmt.Errorf("send body: %w", serr)
				}
			}
			if errors.Is(err, io.EOF) {
				break
			}
			if err != nil {
				return fmt.Errorf("read request body: %w", err)
			}
		}
	}
	if err := p.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_End{End: &hostproto.End{}}}); err != nil {
		return fmt.Errorf("send end: %w", err)
	}
	return nil
}

// respond writes the container's response as it arrives. A 101 hands the
// client's connection to the container.
func (e *Edge) respond(w http.ResponseWriter, r *http.Request, ex *exchangeState) {
	head := ex.first.GetHead()
	if head.GetStatus() == http.StatusSwitchingProtocols {
		e.tunnel(w, ex)
		return
	}
	header := w.Header()
	ours := header.Get(RequestIDHeader)
	for _, h := range head.GetHeaders() {
		header.Add(h.GetName(), h.GetValue())
	}
	if ours != "" {
		header.Set(RequestIDHeader, ours)
	}
	for _, name := range hopHeaders {
		header.Del(name)
	}
	w.WriteHeader(int(head.GetStatus()))
	rc := ex.rc
	_ = rc.Flush()
	complete := false
	for {
		msg, err := ex.stream.stream.Recv()
		if err != nil {
			break
		}
		if data := msg.GetData(); data != nil {
			if _, err := w.Write(data); err != nil { //nolint:gosec // the workload's own response, passed through
				break
			}
			_ = rc.Flush()
			continue
		}
		complete = msg.GetEnd() != nil
		break
	}
	ex.close()
	if !complete && r.Context().Err() == nil {
		// The response broke off after its status was sent; dropping the
		// connection is the only way to tell the client it is incomplete.
		panic(http.ErrAbortHandler)
	}
}

// tunnel completes a protocol upgrade and copies bytes both ways until
// either side closes.
func (e *Edge) tunnel(w http.ResponseWriter, ex *exchangeState) {
	defer ex.close()
	conn, rw, err := http.NewResponseController(w).Hijack()
	if err != nil {
		writeError(w, http.StatusInternalServerError, "the connection cannot be upgraded")
		return
	}
	defer func() { _ = conn.Close() }()
	_, _ = fmt.Fprintf(rw, "HTTP/1.1 101 Switching Protocols\r\n")
	for _, h := range ex.first.GetHead().GetHeaders() {
		if !strings.EqualFold(h.GetName(), RequestIDHeader) {
			_, _ = fmt.Fprintf(rw, "%s: %s\r\n", h.GetName(), h.GetValue())
		}
	}
	if id := w.Header().Get(RequestIDHeader); id != "" {
		_, _ = fmt.Fprintf(rw, "%s: %s\r\n", RequestIDHeader, id)
	}
	_, _ = rw.WriteString("\r\n")
	if err := rw.Flush(); err != nil {
		return
	}
	// The sender sent nothing for an upgrade; client bytes follow now.
	<-ex.sending
	done := make(chan struct{})
	go func() { //nolint:gocritic // waited for below
		defer close(done)
		buf := make([]byte, bodyChunk)
		for {
			n, err := rw.Read(buf)
			if n > 0 {
				chunk := make([]byte, n)
				copy(chunk, buf[:n])
				if ex.stream.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_Data{Data: chunk}}) != nil {
					return
				}
			}
			if err != nil {
				_ = ex.stream.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_End{End: &hostproto.End{}}})
				return
			}
		}
	}()
	for {
		msg, err := ex.stream.stream.Recv()
		if err != nil || msg.GetData() == nil {
			break
		}
		if _, err := conn.Write(msg.GetData()); err != nil {
			break
		}
	}
	_ = conn.Close()
	ex.stream.finish(nil)
	<-done
}

func sleepUntil(ctx context.Context, d time.Duration, deadline time.Time) bool {
	if time.Now().Add(d).After(deadline) {
		return false
	}
	if d <= 0 {
		return true
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
