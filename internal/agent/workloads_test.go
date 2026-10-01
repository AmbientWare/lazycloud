package agent

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// dataServer is the edge's side of HostData: each Forward stream the agent
// opens waits in calls until a test assigns it a request.
type dataServer struct {
	hostproto.UnimplementedHostDataServer
	calls chan *forwardCall
}

type forwardCall struct {
	stream hostproto.HostData_ForwardServer
	done   chan struct{}
}

func (d *dataServer) Listen(_ *hostproto.ListenRequest, stream hostproto.HostData_ListenServer) error {
	<-stream.Context().Done()
	return nil
}

func (d *dataServer) Forward(stream hostproto.HostData_ForwardServer) error {
	call := &forwardCall{stream: stream, done: make(chan struct{})}
	select {
	case d.calls <- call:
	case <-stream.Context().Done():
		return nil
	}
	select {
	case <-call.done:
	case <-stream.Context().Done():
	}
	return nil
}

// forwarded is what the agent answered on one stream.
type forwarded struct {
	status int32
	body   string
	err    *hostproto.ForwardError
}

// forward sends one request on an idle stream and reads the answer.
func (d *dataServer) forward(t *testing.T, head *hostproto.RequestHead, body string) forwarded {
	t.Helper()
	call := d.call(t)
	defer close(call.done)
	send := func(m *hostproto.ForwardDown) {
		if err := call.stream.Send(m); err != nil {
			t.Fatal(err)
		}
	}
	send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_Head{Head: head}})
	if body != "" {
		send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_Data{Data: []byte(body)}})
	}
	send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_End{End: &hostproto.End{}}})
	var out forwarded
	var b strings.Builder
	for {
		m, err := call.stream.Recv()
		if err != nil {
			t.Fatalf("receive response: %v", err)
		}
		switch body := m.GetBody().(type) {
		case *hostproto.ForwardUp_Head:
			out.status = body.Head.GetStatus()
		case *hostproto.ForwardUp_Data:
			b.Write(body.Data)
		case *hostproto.ForwardUp_Error:
			out.err = body.Error
			return out
		case *hostproto.ForwardUp_End:
			out.body = b.String()
			return out
		}
	}
}

func (d *dataServer) call(t *testing.T) *forwardCall {
	t.Helper()
	select {
	case call := <-d.calls:
		return call
	case <-time.After(30 * time.Second):
		t.Fatal("the agent opened no data stream")
		return nil
	}
}

// tunnel sends an upgrade request and returns the stream once the agent
// answered 101, to exchange raw bytes over.
func (d *dataServer) tunnel(t *testing.T, head *hostproto.RequestHead) *forwardCall {
	t.Helper()
	call := d.call(t)
	t.Cleanup(func() { close(call.done) })
	if err := call.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_Head{Head: head}}); err != nil {
		t.Fatal(err)
	}
	m, err := call.stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if m.GetHead().GetStatus() != http.StatusSwitchingProtocols {
		t.Fatalf("upgrade answered %v", m)
	}
	return call
}

func controlHead(container, method, uri string) *hostproto.RequestHead {
	return &hostproto.RequestHead{ContainerId: container, Kind: &hostproto.RequestHead_Control{Control: &hostproto.HttpRequest{Method: method, Uri: uri, Host: "container"}}}
}

func portHead(container string, port int32, method, uri string, headers ...*hostproto.Header) *hostproto.RequestHead {
	return &hostproto.RequestHead{ContainerId: container, Kind: &hostproto.RequestHead_Port{Port: &hostproto.PortRequest{
		Port: port, Http: &hostproto.HttpRequest{Method: method, Uri: uri, Host: "pod.example", Headers: headers},
	}}}
}

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
	dir, err := os.MkdirTemp("", "lca")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	a := &Agent{
		cfg:        Config{StateDir: dir, SocketDir: filepath.Join(dir, "s")},
		log:        slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelDebug})),
		host:       hostproto.NewHostServiceClient(conn),
		ctx:        ctx,
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
		_ = os.RemoveAll(dir)
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
func tunnelTo(t *testing.T, w http.ResponseWriter, address string) {
	t.Helper()
	upstream, err := net.Dial("tcp", address)
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
			tunnelTo(t, w, app.Listener.Addr().String())
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
		got = data.forward(t, portHead(c.id, port, http.MethodGet, "/index.html?q=1"), "")
		if got.status != http.StatusOK || got.body != "port saw GET /index.html?q=1 for pod.example" {
			t.Fatalf("port request: %+v", got)
		}
	}
	if n := tunnels.Load(); n != 1 {
		t.Fatalf("three requests opened %d tunnels", n)
	}

	call := data.tunnel(t, portHead(c.id, port, http.MethodGet, "/ws",
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

	refused := data.forward(t, portHead(c.id, 1, http.MethodGet, "/", nil...), "")
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
	unstarted := data.forward(t, portHead(c.id, port, http.MethodGet, "/"), "")
	if unstarted.err.GetKind() != hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING {
		t.Fatalf("unstarted container: %+v", unstarted)
	}
}

func strconvAtoi32(s string) (int32, error) {
	var n int32
	_, err := fmt.Sscanf(s, "%d", &n)
	return n, err
}

func (s *hostServer) CompleteSnapshot(_ context.Context, r *hostproto.CompleteSnapshotRequest) (*hostproto.CompleteSnapshotResponse, error) {
	s.snapshots <- r
	return &hostproto.CompleteSnapshotResponse{}, nil
}

func (s *hostServer) CompleteFilesystemImage(_ context.Context, r *hostproto.CompleteFilesystemImageRequest) (*hostproto.CompleteFilesystemImageResponse, error) {
	s.filesystems <- r
	return &hostproto.CompleteFilesystemImageResponse{}, nil
}

// podCommand starts a pod running command in the test image with the test
// workspace.
func (e *env) podCommand(pod *hostproto.PodWorkload) *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Start{Start: &hostproto.StartContainer{
		ContainerId: uuid.NewString(),
		Image:       testImage,
		Source:      e.source,
		Resources:   &hostproto.Resources{CpuMillis: 1000, MemoryBytes: 256 << 20},
		Pod:         pod,
		Environment: map[string]string{"PYTHONDONTWRITEBYTECODE": "1"},
	}}}
}

// containerLog joins the output the container logged outside attempts.
func (s *hostServer) containerLog(container string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return strings.Join(s.containerLogs[container], "\n")
}

// A pod runs its command in the source workspace: its port answers through
// the supervisor's tunnel and its files and processes through the control
// API, and it never claims tasks.
func TestPodServesItsPortAndControlAPI(t *testing.T) {
	e := newEnv(t)
	e.startAgent()
	session := e.session()
	start := e.podCommand(&hostproto.PodWorkload{Command: []string{"python3", "-m", "http.server", "8000"}, Ports: []int32{8000}})
	id := start.GetStart().GetContainerId()
	began := time.Now()
	session.send(t, start)
	ready := session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	t.Logf("pod ready after %s; stages %v", time.Since(began), ready.GetStartup())

	listing := e.server.data.forward(t, portHead(id, 8000, http.MethodGet, "/"), "")
	if listing.status != http.StatusOK || !strings.Contains(listing.body, "app.py") {
		t.Fatalf("port 8000: %+v", listing)
	}
	files := e.server.data.forward(t, controlHead(id, http.MethodGet, "/files?path=/workspace"), "")
	if files.status != http.StatusOK || !strings.Contains(files.body, "app.py") {
		t.Fatalf("control files: %+v", files)
	}
	processes := e.server.data.forward(t, controlHead(id, http.MethodGet, "/processes"), "")
	if processes.status != http.StatusOK {
		t.Fatalf("control processes: %+v", processes)
	}
	refused := e.server.data.forward(t, portHead(id, 8001, http.MethodGet, "/"), "")
	if refused.err.GetKind() != hostproto.ForwardErrorKind_FORWARD_ERROR_KIND_NOT_RUNNING {
		t.Fatalf("closed port: %+v", refused)
	}
	session.send(t, stopCommand(id, 5))
	exit := session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED).GetExit()
	if exit.GetReason() != hostproto.ExitReason_EXIT_REASON_STOPPED {
		t.Fatalf("stopped pod exit %v", exit)
	}
}

// A command that ends on its own exits the pod as EXITED with its code and
// its output in the container log; an empty command runs the image's own,
// here python3 reading an empty stdin.
func TestPodCommandExitReportsItsCode(t *testing.T) {
	e := newEnv(t)
	e.startAgent()
	session := e.session()
	failing := e.podCommand(&hostproto.PodWorkload{Command: []string{"sh", "-c", "echo pod says bye; exit 7"}})
	imageOwn := e.podCommand(&hostproto.PodWorkload{})
	session.send(t, failing)
	session.send(t, imageOwn)
	for _, want := range []struct {
		start *hostproto.ServerMessage
		code  int32
	}{{failing, 7}, {imageOwn, 0}} {
		id := want.start.GetStart().GetContainerId()
		exit := session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED).GetExit()
		if exit.GetReason() != hostproto.ExitReason_EXIT_REASON_EXITED || exit.GetExitCode() != want.code {
			t.Fatalf("pod %s exit %v, want EXITED with %d", id, exit, want.code)
		}
	}
	deadline := time.Now().Add(10 * time.Second)
	for !strings.Contains(e.server.containerLog(failing.GetStart().GetContainerId()), "pod says bye") {
		if time.Now().After(deadline) {
			t.Fatalf("pod output missing from the log: %q", e.server.containerLog(failing.GetStart().GetContainerId()))
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// A blocked pod reaches nothing from its first instruction on; an update
// allowing one address opens exactly that, and an update for an exited
// container is ignored.
func TestNetworkPolicyBlocksEgressUntilAllowed(t *testing.T) {
	e := newEnv(t)
	e.startAgent()
	session := e.session()
	target := startTargetServer(t, e.id)

	probe := fmt.Sprintf(`import socket, time
while True:
    try:
        socket.create_connection((%q, 80), timeout=1).close()
        print("reach open", flush=True)
    except OSError:
        print("reach blocked", flush=True)
    time.sleep(0.3)
`, target)
	start := e.podCommand(&hostproto.PodWorkload{Command: []string{"python3", "-c", probe}, Network: &hostproto.NetworkPolicy{Block: true}})
	id := start.GetStart().GetContainerId()
	session.send(t, start)
	session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	waitLog(t, e.server, id, "reach blocked")
	if strings.Contains(e.server.containerLog(id), "reach open") {
		t.Fatal("the pod reached the network before its policy applied")
	}
	session.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Network{Network: &hostproto.UpdateNetwork{
		ContainerId: id, Policy: &hostproto.NetworkPolicy{Allow: []string{target + "/32"}},
	}}})
	waitLog(t, e.server, id, "reach open")
	session.send(t, stopCommand(id, 0))
	session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
	session.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Network{Network: &hostproto.UpdateNetwork{
		ContainerId: id, Policy: &hostproto.NetworkPolicy{Block: true},
	}}})
}

// startTargetServer runs a web server container on the default bridge and
// returns its address.
func startTargetServer(t *testing.T, label string) string {
	t.Helper()
	out, err := exec.CommandContext(t.Context(), "docker", "run", "-d", "--rm", "--label", "lazycloud.agent="+label,
		testBuildBase, "httpd", "-f", "-p", "80").Output()
	if err != nil {
		t.Fatalf("start target: %v", err)
	}
	id := strings.TrimSpace(string(out))
	t.Cleanup(func() { _ = exec.CommandContext(context.Background(), "docker", "rm", "-f", id).Run() })
	ip, err := exec.CommandContext(t.Context(), "docker", "inspect", "-f", "{{.NetworkSettings.IPAddress}}", id).Output()
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(string(ip))
}

func waitLog(t *testing.T, s *hostServer, container, text string) {
	t.Helper()
	deadline := time.Now().Add(30 * time.Second)
	for !strings.Contains(s.containerLog(container), text) {
		if time.Now().After(deadline) {
			t.Fatalf("%q never appeared in the container log: %q", text, s.containerLog(container))
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// runc without CRIU cannot checkpoint: a snapshot reports unsupported with
// the runtime's reason and the container keeps running.
func TestSnapshotOnRuncWithoutCRIUIsUnsupported(t *testing.T) {
	e := newEnv(t)
	e.startAgent()
	session := e.session()
	start := e.startCommand("app:handle", 1)
	id := start.GetStart().GetContainerId()
	session.send(t, start)
	session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	uploads := make(chan struct{}, 1)
	store := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { uploads <- struct{}{} }))
	t.Cleanup(store.Close)
	snapshotID := uuid.NewString()
	session.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Snapshot{Snapshot: &hostproto.SnapshotContainer{
		ContainerId: id, SnapshotId: snapshotID, UploadUrl: store.URL + "/snapshot", Deadline: timestamppb.New(time.Now().Add(time.Minute)),
	}}})
	select {
	case r := <-e.server.snapshots:
		if r.GetSnapshotId() != snapshotID || !r.GetUnsupported() || !strings.Contains(strings.ToLower(r.GetFailure()), "criu") {
			t.Fatalf("snapshot outcome %v", r)
		}
	case <-time.After(60 * time.Second):
		t.Fatal("no snapshot outcome")
	}
	select {
	case <-uploads:
		t.Fatal("an unsupported snapshot uploaded something")
	default:
	}
	attempt := e.task(id, `{"args": ["sleep", 0]}`)
	if r := e.completion(attempt); r.GetSuccess() == nil {
		t.Fatalf("the container stopped serving after the snapshot: %v", r)
	}
	if _, err := os.Stat(filepath.Join(e.stateDir, "snapshots", snapshotID)); !os.IsNotExist(err) {
		t.Fatalf("the snapshot directory remains: %v", err)
	}
}

// An automatic snapshot that does not restore starts the container cold and
// says so; a requested one fails the start.
func TestFailedRestoreStartsColdOnlyForAutomaticSnapshots(t *testing.T) {
	e := newEnv(t)
	e.startAgent()
	session := e.session()
	archive := tarOf(t, map[string]string{"inventory.img": "not a checkpoint"})
	sum := sha256.Sum256(archive)
	store := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { _, _ = w.Write(archive) }))
	t.Cleanup(store.Close)
	restore := func(automatic bool) *hostproto.SnapshotRestore {
		return &hostproto.SnapshotRestore{SnapshotId: uuid.NewString(), Url: store.URL, Sha256: hex.EncodeToString(sum[:]), Automatic: automatic}
	}

	automatic := e.startCommand("app:handle", 1)
	automatic.GetStart().Restore = restore(true)
	session.send(t, automatic)
	ready := session.phase(t, automatic.GetStart().GetContainerId(), hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	if ready.GetRestoreFailed() != automatic.GetStart().GetRestore().GetSnapshotId() {
		t.Fatalf("cold start report %v", ready)
	}
	attempt := e.task(automatic.GetStart().GetContainerId(), `{"args": ["sleep", 0]}`)
	if r := e.completion(attempt); r.GetSuccess() == nil {
		t.Fatalf("the cold container did not run: %v", r)
	}

	requested := e.startCommand("app:handle", 1)
	requested.GetStart().Restore = restore(false)
	session.send(t, requested)
	exit := session.phase(t, requested.GetStart().GetContainerId(), hostproto.ContainerPhase_CONTAINER_PHASE_EXITED).GetExit()
	if exit.GetReason() != hostproto.ExitReason_EXIT_REASON_START_FAILED || !strings.Contains(exit.GetMessage(), "restore snapshot") {
		t.Fatalf("failed restore exit %v", exit)
	}

	corrupt := e.startCommand("app:handle", 1)
	corrupt.GetStart().Restore = restore(false)
	corrupt.GetStart().Restore.Sha256 = strings.Repeat("0", 64)
	session.send(t, corrupt)
	exit = session.phase(t, corrupt.GetStart().GetContainerId(), hostproto.ContainerPhase_CONTAINER_PHASE_EXITED).GetExit()
	if !strings.Contains(exit.GetMessage(), "digest mismatch") {
		t.Fatalf("corrupt snapshot exit %v", exit)
	}
}

// A pod's filesystem becomes an image in the registry with the source
// image's configuration, and the local copy is gone afterwards.
func TestPublishFilesystemPushesAnImage(t *testing.T) {
	registry := startTestRegistry(t)
	e := newEnv(t)
	e.startAgent()
	session := e.session()
	start := e.podCommand(&hostproto.PodWorkload{Command: []string{"sh", "-c", "echo published > /proof && exec sleep 600"}})
	id := start.GetStart().GetContainerId()
	session.send(t, start)
	session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	requestID := uuid.NewString()
	repository := registry + "/lazycloud/filesystems"
	began := time.Now()
	session.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_PublishFilesystem{PublishFilesystem: &hostproto.PublishFilesystem{
		ContainerId: id, RequestId: requestID, Repository: repository, InsecureRegistry: true, Deadline: timestamppb.New(time.Now().Add(5 * time.Minute)),
	}}})
	var result *hostproto.CompleteFilesystemImageRequest
	select {
	case result = <-e.server.filesystems:
	case <-time.After(5 * time.Minute):
		t.Fatal("no filesystem outcome")
	}
	if result.GetFailure() != "" || !strings.HasPrefix(result.GetReference(), repository+"@sha256:") || result.GetArchitecture() == "" {
		t.Fatalf("filesystem outcome %v", result)
	}
	t.Logf("published %s in %s", result.GetReference(), time.Since(began))
	if out, err := exec.CommandContext(t.Context(), "docker", "image", "inspect", repository+":fs-"+requestID).CombinedOutput(); err == nil {
		t.Fatalf("the local image remains: %s", out)
	}
	t.Cleanup(func() {
		_ = exec.CommandContext(context.Background(), "docker", "rmi", "-f", result.GetReference()).Run()
	})
	out, err := exec.CommandContext(t.Context(), "docker", "run", "--rm", "--label", "lazycloud.agent="+e.id, result.GetReference(), "cat", "/proof").CombinedOutput()
	if err != nil || strings.TrimSpace(string(out)) != "published" {
		t.Fatalf("run the published image: %v: %s", err, out)
	}
	config, err := exec.CommandContext(t.Context(), "docker", "image", "inspect", "-f", "{{json .Config.Cmd}} {{.Config.WorkingDir}}", result.GetReference()).Output()
	if err != nil || !strings.Contains(string(config), `["python3"]`) {
		t.Fatalf("published image config %s: %v", config, err)
	}
}

func tarOf(t *testing.T, files map[string]string) []byte {
	t.Helper()
	dir := t.TempDir()
	for name, content := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	var b strings.Builder
	if err := tarDir(&b, dir); err != nil {
		t.Fatal(err)
	}
	return []byte(b.String())
}

// The handshake reader keeps bytes the supervisor sent right after its 101.
func TestPortTunnelKeepsBytesSentWithTheUpgrade(t *testing.T) {
	dir, err := os.MkdirTemp("", "lca")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	socket := filepath.Join(dir, controlSocketName)
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
