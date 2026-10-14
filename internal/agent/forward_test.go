package agent

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// newForwardingAgent runs a data link for one started container without
// Docker, whose control socket a test serves.
func newForwardingAgent(t *testing.T) (*dataServer, *container) {
	t.Helper()
	server := newHostServer()
	grpcServer, address := server.serve(t, "127.0.0.1:0")
	conn, err := dialServer(Config{Server: address, ServerPlaintext: true}, server.hostToken)
	if err != nil {
		t.Fatal(err)
	}
	dir := shortDir(t)
	layers, _ := testLayerSources(t)
	ctx, cancel := context.WithCancel(context.Background())
	a := &Agent{
		cfg:        Config{StateDir: dir, SocketDir: filepath.Join(dir, "s")},
		log:        slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug})),
		host:       hostproto.NewHostServiceClient(conn),
		layers:     layers,
		ctx:        ctx,
		drain:      ctx,
		containers: map[string]*container{},
	}
	c := a.newContainer(uuid.NewString(), "", 1, nil, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	c.started = true
	a.containers[c.id] = c
	data := &dataLink{a: a, client: hostproto.NewHostDataClient(conn)}
	a.goOwned(data.run)
	t.Cleanup(func() {
		cancel()
		a.work.Wait()
		_ = conn.Close()
		grpcServer.Stop()
	})
	if err := os.MkdirAll(c.linkDir(), 0o700); err != nil {
		t.Fatal(err)
	}
	return server.data, c
}

// serveControl serves handler on the container's control socket, as its
// supervisor does.
func serveControl(t *testing.T, c *container, handler http.Handler) {
	t.Helper()
	listener, err := (&net.ListenConfig{}).Listen(t.Context(), "unix", filepath.Join(c.linkDir(), controlSocketName))
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: handler, ReadHeaderTimeout: 5 * time.Second}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { _ = server.Close() })
}

// tunnelTo answers a lazycloud-tunnel upgrade by joining the connection to
// address, as the supervisor's port tunnels do.
func tunnelTo(t *testing.T, w http.ResponseWriter, r *http.Request, address string) {
	t.Helper()
	upstream, err := (&net.Dialer{}).DialContext(r.Context(), "tcp", address)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadGateway)
		return
	}
	conn, buffered, err := http.NewResponseController(w).Hijack()
	if err != nil {
		t.Error(err)
		return
	}
	_, _ = buffered.WriteString("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: " + tunnelProtocol + "\r\n\r\n")
	_ = buffered.Flush()
	go func() {
		_, _ = io.Copy(upstream, buffered)
		_ = upstream.(*net.TCPConn).CloseWrite()
	}()
	_, _ = io.Copy(conn, upstream)
	_ = conn.Close()
	_ = upstream.Close()
}

// Control requests reach the container's control socket with their bodies;
// port requests reach the port through a supervisor tunnel that later
// requests reuse, and a client upgrade still tunnels. A refused port, a
// container not running here and an unstarted one answer NOT_RUNNING, since
// nothing reached a workload.
func TestControlAndPortRequestsReachTheSupervisorSocket(t *testing.T) {
	t.Parallel()
	data, c := newForwardingAgent(t)
	app := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Upgrade") == "echo" {
			conn, buffered, _ := http.NewResponseController(w).Hijack()
			_, _ = buffered.WriteString("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: echo\r\n\r\n")
			_ = buffered.Flush()
			line, _ := buffered.ReadString('\n')
			_, _ = conn.Write([]byte("echo " + line))
			_ = conn.Close()
			return
		}
		_, _ = fmt.Fprintf(w, "port saw %s %s for %s", r.Method, r.URL.RequestURI(), r.Host)
	}))
	t.Cleanup(app.Close)
	_, appPort, _ := net.SplitHostPort(app.Listener.Addr().String())
	var tunnels atomic.Int32
	serveControl(t, c, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/ports/"+appPort && r.Header.Get("Upgrade") == tunnelProtocol:
			tunnels.Add(1)
			tunnelTo(t, w, r, app.Listener.Addr().String())
		case strings.HasPrefix(r.URL.Path, "/ports/"):
			http.Error(w, `{"code":"unavailable","message":"connection refused"}`, http.StatusBadGateway)
		default:
			body, _ := io.ReadAll(r.Body)
			_, _ = fmt.Fprintf(w, "control saw %s %s %s", r.Method, r.URL.RequestURI(), body)
		}
	}))

	got := data.forward(t, controlHead(c.id, http.MethodPost, "/files/find?path=/workspace"), `{"pattern":"x"}`)
	if got.status != http.StatusOK || got.body != `control saw POST /files/find?path=/workspace {"pattern":"x"}` {
		t.Fatalf("control request: %+v", got)
	}
	port, _ := strconvAtoi32(appPort)
	for range 3 {
		got = data.forward(t, portHead(c.id, port, "/index.html?q=1"), "")
		if got.status != http.StatusOK || got.body != "port saw GET /index.html?q=1 for pod.example" {
			t.Fatalf("port request: %+v", got)
		}
	}
	if n := tunnels.Load(); n != 1 {
		t.Fatalf("three requests opened %d tunnels", n)
	}

	call := data.tunnel(t, portHead(c.id, port, "/ws",
		&hostproto.Header{Name: "Connection", Value: "Upgrade"}, &hostproto.Header{Name: "Upgrade", Value: "echo"}))
	if err := call.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_Data{Data: []byte("hello\n")}}); err != nil {
		t.Fatal(err)
	}
	var echoed strings.Builder
	for !strings.HasSuffix(echoed.String(), "\n") {
		m, err := call.stream.Recv()
		if err != nil {
			t.Fatalf("tunnel: %v after %q", err, echoed.String())
		}
		echoed.Write(m.GetData())
	}
	if echoed.String() != "echo hello\n" {
		t.Fatalf("tunnel echoed %q", echoed.String())
	}

	refused := data.forward(t, portHead(c.id, 1, "/", nil...), "")
	if refused.err.GetKind() != hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING || !strings.Contains(refused.err.GetMessage(), "connection refused") {
		t.Fatalf("refused port: %+v", refused)
	}
	unknown := data.forward(t, controlHead(uuid.NewString(), http.MethodGet, "/processes"), "")
	if unknown.err.GetKind() != hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING {
		t.Fatalf("unknown container: %+v", unknown)
	}
	c.mu.Lock()
	c.started = false
	c.mu.Unlock()
	unstarted := data.forward(t, portHead(c.id, port, "/"), "")
	if unstarted.err.GetKind() != hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING {
		t.Fatalf("unstarted container: %+v", unstarted)
	}
}

// A workload that swaps its supervisor's socket for a symlink reaches
// nothing through it: on the host the link would lead to another socket.
func TestSupervisorSocketsAreNeverReachedThroughALink(t *testing.T) {
	t.Parallel()
	data, c := newForwardingAgent(t)
	outside := filepath.Join(t.TempDir(), "other.sock")
	listener, err := (&net.ListenConfig{}).Listen(t.Context(), "unix", outside)
	if err != nil {
		t.Fatal(err)
	}
	var reached atomic.Int32
	server := &http.Server{ReadHeaderTimeout: 5 * time.Second, Handler: http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		reached.Add(1)
	})}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { _ = server.Close() })
	if err := os.Symlink(outside, filepath.Join(c.linkDir(), controlSocketName)); err != nil {
		t.Fatal(err)
	}
	for _, head := range []*hostproto.RequestHead{controlHead(c.id, http.MethodGet, "/processes"), portHead(c.id, 8000, "/")} {
		got := data.forward(t, head, "")
		if got.err.GetKind() != hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING || !strings.Contains(got.err.GetMessage(), "not a socket") {
			t.Fatalf("through a link: %+v %v", got, got.err)
		}
	}
	if n := reached.Load(); n != 0 {
		t.Fatalf("the linked socket answered %d requests", n)
	}
}

// A filesystem archive past what the container can store fails the read.
func TestFilesystemArchiveStopsAtTheContainersDisks(t *testing.T) {
	t.Parallel()
	_, c := newForwardingAgent(t)
	archive := &countingReader{r: strings.NewReader(strings.Repeat("a", 4096)), max: c.archiveBound(map[string]string{"size": "1024"})}
	if _, err := io.Copy(io.Discard, archive); !errors.Is(err, errArchiveTooLarge) {
		t.Fatalf("an oversized archive read %d bytes: %v", archive.n, err)
	}
}

func strconvAtoi32(s string) (int32, error) {
	var n int32
	_, err := fmt.Sscanf(s, "%d", &n)
	return n, err
}

// The handshake reader keeps bytes the supervisor sent right after its 101.
func TestPortTunnelKeepsBytesSentWithTheUpgrade(t *testing.T) {
	t.Parallel()
	socket := filepath.Join(shortDir(t), controlSocketName)
	listener, err := (&net.ListenConfig{}).Listen(t.Context(), "unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = listener.Close() })
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		_, _ = http.ReadRequest(bufio.NewReader(conn))
		_, _ = io.WriteString(conn, "HTTP/1.1 101 Switching Protocols\r\nUpgrade: "+tunnelProtocol+"\r\nConnection: Upgrade\r\n\r\nearly")
		_ = conn.Close()
	}()
	conn, err := dialPort(t.Context(), socket, 8000)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = conn.Close() }()
	got, _ := io.ReadAll(conn)
	if string(got) != "early" {
		t.Fatalf("read %q after the upgrade", got)
	}
}
