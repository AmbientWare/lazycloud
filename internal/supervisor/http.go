package supervisor

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/runnerproto"
)

// BusyHeader marks the supervisor's own 503: every worker of the container is
// at its concurrency, or the container is draining or reloading. Nothing
// reached the workload, so the caller may send the request elsewhere.
const BusyHeader = "Lazycloud-Busy"

// Busy reasons in BusyHeader.
const (
	BusyFull     = "full"
	BusyDraining = "draining"
)

// serveHTTP loads the handler, then lets the front forward requests to the
// runner until it exits (restart), the container drains (nil) or a reload
// asks for a fresh runner (restart). Draining and reloading stop admitting
// and wait for the requests in flight.
func (sl *slot) serveHTTP(ctx context.Context, p *runnerProcess, cfg *hostproto.Configure) (bool, error) {
	stop := context.AfterFunc(ctx, p.terminate)
	defer stop()
	if loadErr := p.load(cfg, 1); loadErr != nil {
		if ctx.Err() != nil {
			return false, fmt.Errorf("load handler: %w", ctx.Err())
		}
		sl.sup.loadFailed(loadErr)
		return false, ErrLoadFailed
	}
	// stop closes the runner's socket, which ends the reader, then waits
	// for it.
	p.readers.Go(func() { sl.readHTTPOutput(ctx, p) })
	sl.http.open(p.httpAddr, max(1, int(cfg.GetHttp().GetConcurrency())))
	defer sl.http.stopAccepting()
	sl.sup.slotLoaded(sl)
	loaded := time.Now()
	select {
	case <-ctx.Done():
		return false, fmt.Errorf("serve requests: %w", ctx.Err())
	case <-p.done:
		sl.http.stopAccepting()
		if !sleepCtx(ctx, time.Second-time.Since(loaded)) {
			return false, fmt.Errorf("restart runner: %w", ctx.Err())
		}
		return true, nil
	case <-sl.sup.drain:
		sl.http.stopAccepting()
		sl.http.waitIdle(ctx, p.done)
		return false, nil
	case <-sl.reload:
		sl.http.stopAccepting()
		sl.http.waitIdle(ctx, p.done)
		return true, nil
	}
}

// readHTTPOutput sends what an HTTP worker writes while serving requests,
// which arrives as output frames, one line per message under the request's
// id, until the runner closes its socket.
func (sl *slot) readHTTPOutput(ctx context.Context, p *runnerProcess) {
	// held is output per request and stream that may begin a secret value
	// continued in the next frame. Only this goroutine touches it.
	type heldKey struct {
		request string
		stream  hostproto.LogStream
	}
	held := map[heldKey]string{}
	for {
		frame, err := p.read()
		if err != nil {
			break
		}
		if frame.Type != runnerproto.FrameOutput {
			continue
		}
		var out runnerproto.Output
		if err := frame.Decode(&out); err != nil || out.RequestId == nil {
			continue
		}
		stream := hostproto.LogStream_LOG_STREAM_STDOUT
		if out.Stream == runnerproto.Stderr {
			stream = hostproto.LogStream_LOG_STREAM_STDERR
		}
		key := heldKey{*out.RequestId, stream}
		pending := held[key]
		text := sl.sup.redact.stream(&pending, strings.ToValidUTF8(string(frame.Payload), "\uFFFD"), false)
		if pending == "" {
			delete(held, key)
		} else {
			held[key] = pending
		}
		sl.sup.pushRequestOutput(key.request, stream, text)
		if sl.sup.out.waitOutputSpace(ctx) != nil {
			return
		}
	}
	for key, text := range held {
		sl.sup.pushRequestOutput(key.request, key.stream, text)
	}
}

// pushRequestOutput queues text a request's handler wrote, a line per
// message without its newline.
func (s *Supervisor) pushRequestOutput(request string, stream hostproto.LogStream, text string) {
	text = strings.TrimSuffix(text, "\n")
	if text == "" {
		return
	}
	for line := range strings.SplitSeq(text, "\n") {
		s.out.push(&hostproto.SupervisorMessage{Body: &hostproto.SupervisorMessage_Output{Output: &hostproto.OutputChunk{
			RequestId: request, Stream: stream, Data: line, Time: timestamppb.Now(),
		}}})
	}
}

// httpFront serves HTTP on the container's socket and forwards each request
// to the worker with the fewest requests in flight.
type httpFront struct {
	sup         *Supervisor
	concurrency int
	listener    net.Listener
	server      *http.Server
}

func (s *Supervisor) listenHTTP(ctx context.Context, cfg *hostproto.Configure) (*httpFront, error) {
	path := cfg.GetHttpSocket()
	if path == "" {
		return nil, errors.New("configure has http but no http_socket")
	}
	if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("remove stale http socket: %w", err)
	}
	listener, err := (&net.ListenConfig{}).Listen(ctx, "unix", path)
	if err != nil {
		return nil, fmt.Errorf("listen on http socket: %w", err)
	}
	// The agent connects from the host, possibly as another user.
	if err := os.Chmod(path, 0o666); err != nil { //nolint:gosec // only the agent reaches this directory
		_ = listener.Close()
		return nil, fmt.Errorf("chmod http socket: %w", err)
	}
	f := &httpFront{sup: s, concurrency: max(1, int(cfg.GetHttp().GetConcurrency())), listener: listener}
	f.server = &http.Server{Handler: f, ReadHeaderTimeout: 30 * time.Second}
	return f, nil
}

func (f *httpFront) serve() error {
	if err := f.server.Serve(f.listener); !errors.Is(err, http.ErrServerClosed) {
		return fmt.Errorf("serve http: %w", err)
	}
	return nil
}

// close stops accepting; the slots have already finished their requests.
func (f *httpFront) close() {
	_ = f.server.Close()
}

func (f *httpFront) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if f.sup.isDraining() {
		w.Header().Set(BusyHeader, BusyDraining)
		http.Error(w, "the container is draining", http.StatusServiceUnavailable)
		return
	}
	worker := f.sup.workers.acquire(f.concurrency)
	if worker == nil {
		w.Header().Set(BusyHeader, BusyFull)
		http.Error(w, "every worker of the container is busy", http.StatusServiceUnavailable)
		return
	}
	defer worker.release()
	// The workload may answer before it read the whole body, and a
	// streamed body arrives while the answer goes back.
	rc := http.NewResponseController(w)
	_ = rc.EnableFullDuplex()
	if r.Body != nil && r.Body != http.NoBody {
		r.Body = &requestBody{ReadCloser: r.Body, rc: rc}
	}
	worker.proxy.ServeHTTP(w, r)
}

// requestBody is the agent's request body as the proxy reads it. A read
// may block on a client that stopped sending after the workload answered,
// and holds the server's body lock while it does; closing the body before
// its end sets a read deadline in the past, which ends that read, rather
// than wait for the lock.
type requestBody struct {
	io.ReadCloser
	rc     *http.ResponseController
	mu     sync.Mutex
	sawEOF bool
}

func (b *requestBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	if errors.Is(err, io.EOF) {
		b.mu.Lock()
		b.sawEOF = true
		b.mu.Unlock()
	}
	return n, err //nolint:wrapcheck // a body's EOF must pass through unwrapped
}

func (b *requestBody) Close() error {
	b.mu.Lock()
	ended := b.sawEOF
	b.mu.Unlock()
	if ended {
		return b.ReadCloser.Close() //nolint:wrapcheck // passed through
	}
	_ = b.rc.SetReadDeadline(time.Now())
	return nil
}

// httpWorker is the serving state of one slot's runner. Its fields are
// guarded by workers.mu, one lock for every worker, so admission compares
// loads in one pass.
type httpWorker struct {
	workers   *httpWorkers
	accepting bool
	inflight  int
	proxy     *httputil.ReverseProxy
	// idle receives a value when inflight drops to zero.
	idle chan struct{}
}

// httpWorkers is the set of a container's workers.
type httpWorkers struct {
	mu   sync.Mutex
	list []*httpWorker
	// next numbers the runners' listening sockets.
	next uint64
}

func (w *httpWorkers) add() *httpWorker {
	h := &httpWorker{workers: w, idle: make(chan struct{}, 1)}
	w.mu.Lock()
	w.list = append(w.list, h)
	w.mu.Unlock()
	return h
}

// listener creates a runner's listening socket with a unique name, so a
// replacement never collides with a socket an exited runner's descendants
// still hold. The leading @ is Linux's abstract namespace: the socket lives
// as long as a descriptor does and needs no filesystem.
func (w *httpWorkers) listener() (*net.UnixListener, string, error) {
	w.mu.Lock()
	w.next++
	n := w.next
	w.mu.Unlock()
	addr := "@lazycloud-worker-" + strconv.Itoa(os.Getpid()) + "-" + strconv.FormatUint(n, 10)
	listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: addr, Net: "unix"})
	if err != nil {
		return nil, "", fmt.Errorf("listen for a worker: %w", err)
	}
	return listener, addr, nil
}

// acquire admits a request to the accepting worker with the fewest in
// flight, or names why none can take it.
func (w *httpWorkers) acquire(concurrency int) *httpWorker {
	w.mu.Lock()
	defer w.mu.Unlock()
	var best *httpWorker
	for _, h := range w.list {
		if h.accepting && h.inflight < concurrency && (best == nil || h.inflight < best.inflight) {
			best = h
		}
	}
	if best != nil {
		best.inflight++
	}
	return best
}

// open starts admitting requests to a runner listening at addr.
func (h *httpWorker) open(addr string, concurrency int) {
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", addr)
		},
		MaxIdleConnsPerHost: concurrency,
		IdleConnTimeout:     90 * time.Second,
		DisableCompression:  true,
	}
	target := &url.URL{Scheme: "http", Host: "worker"}
	proxy := &httputil.ReverseProxy{
		Rewrite: func(pr *httputil.ProxyRequest) {
			pr.SetURL(target)
			// The edge set these for the original client.
			pr.Out.Host = pr.In.Host
			for _, name := range []string{"X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto"} {
				if values := pr.In.Header.Values(name); len(values) > 0 {
					pr.Out.Header[name] = values
				}
			}
		},
		Transport:     transport,
		FlushInterval: -1,
		ModifyResponse: func(resp *http.Response) error {
			resp.Header.Del(BusyHeader)
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, err error) {
			var opErr *net.OpError
			if errors.As(err, &opErr) && opErr.Op == "dial" {
				// The runner went away before the request reached it.
				w.Header().Set(BusyHeader, BusyFull)
				http.Error(w, "the worker is restarting", http.StatusServiceUnavailable)
				return
			}
			http.Error(w, "the worker failed: "+err.Error(), http.StatusBadGateway)
		},
	}
	h.workers.mu.Lock()
	h.proxy, h.accepting = proxy, true
	h.workers.mu.Unlock()
}

// stopAccepting refuses new requests; requests in flight finish.
func (h *httpWorker) stopAccepting() {
	h.workers.mu.Lock()
	h.accepting = false
	h.workers.mu.Unlock()
}

func (h *httpWorker) release() {
	h.workers.mu.Lock()
	h.inflight--
	idle := h.inflight == 0
	h.workers.mu.Unlock()
	if idle {
		select {
		case h.idle <- struct{}{}:
		default:
		}
	}
}

// waitIdle returns once no request is in flight, the runner exited or ctx
// ended. Call it after stopAccepting.
func (h *httpWorker) waitIdle(ctx context.Context, exited <-chan struct{}) {
	for {
		h.workers.mu.Lock()
		n := h.inflight
		h.workers.mu.Unlock()
		if n == 0 {
			return
		}
		select {
		case <-h.idle:
		case <-exited:
			return
		case <-ctx.Done():
			return
		}
	}
}
