package agent

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
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
	call := d.assign(t, head)
	defer close(call.done)
	send := func(m *hostproto.ForwardDown) {
		if err := call.stream.Send(m); err != nil {
			t.Fatal(err)
		}
	}
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

// assign sends head on an idle stream of a live agent; streams of a
// stopped agent may still wait in calls.
func (d *dataServer) assign(t *testing.T, head *hostproto.RequestHead) *forwardCall {
	t.Helper()
	deadline := time.After(30 * time.Second)
	for {
		select {
		case call := <-d.calls:
			if call.stream.Context().Err() == nil && call.stream.Send(&hostproto.ForwardDown{Body: &hostproto.ForwardDown_Head{Head: head}}) == nil {
				return call
			}
			close(call.done)
		case <-deadline:
			t.Fatal("the agent opened no data stream")
			return nil
		}
	}
}

// tunnel sends an upgrade request and returns the stream once the agent
// answered 101, to exchange raw bytes over.
func (d *dataServer) tunnel(t *testing.T, head *hostproto.RequestHead) *forwardCall {
	t.Helper()
	call := d.assign(t, head)
	t.Cleanup(func() { close(call.done) })
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

func portHead(container string, port int32, uri string, headers ...*hostproto.Header) *hostproto.RequestHead {
	return &hostproto.RequestHead{ContainerId: container, Kind: &hostproto.RequestHead_Port{Port: &hostproto.PortRequest{
		Port: port, Http: &hostproto.HttpRequest{Method: http.MethodGet, Uri: uri, Host: "pod.example", Headers: headers},
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

// The agent's link directory lets a container user other than the agent's
// create the supervisor's sockets, as when a root agent runs a non-root
// image.
func TestNonRootContainerUsersCreateTheirSupervisorSockets(t *testing.T) {
	_, c := newForwardingAgent(t)
	l, err := listenLink(t.Context(), c, c.linkDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { l.close(time.Second) })
	out, err := exec.CommandContext(t.Context(), "docker", "run", "--rm", "--runtime", testRuntime(), "--user", "65534:65534", "--label", "lazycloud.agent="+c.id,
		"-v", c.linkDir()+":"+containerLinkDir, testImage, "python3", "-c",
		"import socket; s = socket.socket(socket.AF_UNIX); s.bind('"+containerControlSocket+"'); print('bound')").CombinedOutput()
	if err != nil || !strings.Contains(string(out), "bound") {
		t.Fatalf("a non-root container user could not create its socket: %v: %s", err, out)
	}
}

// A filesystem archive past what the container can store fails the read.
func TestFilesystemArchiveStopsAtTheContainersDisks(t *testing.T) {
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
	// Each stage is reported as it ends, while the container prepares.
	session.until(t, time.Minute, func(m *hostproto.HostMessage) bool {
		r := m.GetContainer()
		return r.GetContainerId() == id && r.GetPhase() == hostproto.ContainerPhase_CONTAINER_PHASE_PREPARING &&
			len(r.GetStartup()) == 1 && r.GetStartup()[0].GetKind() == hostproto.StartupStageKind_STARTUP_STAGE_KIND_IMAGE
	})
	ready := session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	t.Logf("pod ready after %s; stages %v", time.Since(began), ready.GetStartup())

	listing := e.server.data.forward(t, portHead(id, 8000, "/"), "")
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
	refused := e.server.data.forward(t, portHead(id, 8001, "/"), "")
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
	// The two exit in either order.
	want := map[string]int32{failing.GetStart().GetContainerId(): 7, imageOwn.GetStart().GetContainerId(): 0}
	exits := map[string]*hostproto.ContainerExit{}
	session.until(t, 120*time.Second, func(m *hostproto.HostMessage) bool {
		r := m.GetContainer()
		if _, ok := want[r.GetContainerId()]; ok && r.GetPhase() == hostproto.ContainerPhase_CONTAINER_PHASE_EXITED {
			exits[r.GetContainerId()] = r.GetExit()
		}
		return len(exits) == len(want)
	})
	for id, code := range want {
		if exit := exits[id]; exit.GetReason() != hostproto.ExitReason_EXIT_REASON_EXITED || exit.GetExitCode() != code {
			t.Fatalf("pod %s exit %v, want EXITED with %d", id, exit, code)
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
// allowing one address opens exactly that once the host reports it, and an
// update for an exited container is ignored.
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
		ContainerId: id, Policy: &hostproto.NetworkPolicy{Allow: []string{target + "/32"}}, Version: 1,
	}}})
	// The host reports the version once the filter is in place.
	session.until(t, 30*time.Second, func(m *hostproto.HostMessage) bool {
		r := m.GetContainer()
		return r.GetContainerId() == id && r.GetNetworkVersion() == 1 && r.GetNetworkError() == ""
	})
	waitLog(t, e.server, id, "reach open")
	session.send(t, stopCommand(id, 0))
	session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
	session.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Network{Network: &hostproto.UpdateNetwork{
		ContainerId: id, Policy: &hostproto.NetworkPolicy{Block: true},
	}}})
}

// A pod adopted by a restarted agent is configured as it started: its
// command keeps serving and its SSH server keeps its identity, whose host
// key the agent kept out of the container's labels.
func TestAdoptedPodKeepsItsConfiguration(t *testing.T) {
	e := newEnv(t)
	first := e.startAgent()
	session := e.session()
	start := e.podCommand(&hostproto.PodWorkload{
		Command: []string{"python3", "-m", "http.server", "8000"}, Ports: []int32{8000},
		Ssh: &hostproto.SshServer{HostKey: sshKey(t, "host"), UserAuthority: string(sshPublicKey(t, "authority"))},
	})
	id := start.GetStart().GetContainerId()
	session.send(t, start)
	session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	labels, err := exec.CommandContext(t.Context(), "docker", "inspect", "-f", "{{json .Config.Labels}}", "lazycloud-"+id).Output()
	if err != nil || strings.Contains(string(labels), "PRIVATE KEY") || !strings.Contains(string(labels), labelPod) {
		t.Fatalf("pod labels %s: %v", labels, err)
	}
	first.stop()

	e.startAgent()
	session = e.session()
	session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	if listing := e.server.data.forward(t, portHead(id, 8000, "/"), ""); listing.status != http.StatusOK || !strings.Contains(listing.body, "app.py") {
		t.Fatalf("port 8000 after adoption: %+v", listing)
	}
	head := controlHead(id, http.MethodGet, "/ssh")
	head.GetControl().Headers = []*hostproto.Header{{Name: "Connection", Value: "Upgrade"}, {Name: "Upgrade", Value: tunnelProtocol}}
	call := e.server.data.tunnel(t, head)
	m, err := call.stream.Recv()
	if err != nil || !strings.HasPrefix(string(m.GetData()), "SSH-2.0-") {
		t.Fatalf("SSH tunnel after adoption: %v %v", m, err)
	}
}

// sshKey is a new ed25519 private key in OpenSSH PEM form.
func sshKey(t *testing.T, name string) []byte {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if out, err := exec.CommandContext(t.Context(), "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", path).CombinedOutput(); err != nil {
		t.Fatalf("ssh-keygen: %v: %s", err, out)
	}
	key, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return key
}

// sshPublicKey is a new ed25519 public key as an authorized_keys line.
func sshPublicKey(t *testing.T, name string) []byte {
	t.Helper()
	path := filepath.Join(t.TempDir(), name)
	if out, err := exec.CommandContext(t.Context(), "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", path).CombinedOutput(); err != nil {
		t.Fatalf("ssh-keygen: %v: %s", err, out)
	}
	key, err := os.ReadFile(path + ".pub")
	if err != nil {
		t.Fatal(err)
	}
	return key
}

// testDockerImage has dockerd and the docker CLI.
const testDockerImage = "docker.io/library/docker:28.5.1-dind@sha256:ea9d20492ca1caaaba78e68453433895d256173c79281756e88b745647fcbcfd"

// A runc host runs no Docker daemon unless its operator allowed privileged
// containers, nor one beside a network policy the nested containers would
// bypass.
func TestDockerPodsNeedPrivilegeAllowedAndNoPolicy(t *testing.T) {
	e := newEnv(t)
	e.startAgent(func(c *Config) { c.OCIRuntime = "runc" })
	session := e.session()
	start := e.podCommand(&hostproto.PodWorkload{Command: []string{"docker", "info"}})
	start.GetStart().Docker = true
	session.send(t, start)
	exit := session.phase(t, start.GetStart().GetContainerId(), hostproto.ContainerPhase_CONTAINER_PHASE_EXITED).GetExit()
	if exit.GetReason() != hostproto.ExitReason_EXIT_REASON_START_FAILED || !strings.Contains(exit.GetMessage(), "allow-privileged-docker") {
		t.Fatalf("docker pod on runc exit %v", exit)
	}

	allowed := newEnv(t)
	allowed.startAgent(func(c *Config) { c.AllowPrivilegedDocker = true })
	session = allowed.session()
	start = allowed.podCommand(&hostproto.PodWorkload{Command: []string{"docker", "info"}, Network: &hostproto.NetworkPolicy{Block: true}})
	start.GetStart().Docker = true
	session.send(t, start)
	exit = session.phase(t, start.GetStart().GetContainerId(), hostproto.ContainerPhase_CONTAINER_PHASE_EXITED).GetExit()
	if exit.GetReason() != hostproto.ExitReason_EXIT_REASON_START_FAILED || !strings.Contains(exit.GetMessage(), "bypass the policy") {
		t.Fatalf("docker pod with a policy exit %v", exit)
	}
}

// A pod with docker runs its command against a Docker daemon of its own.
func TestDockerPodRunsADaemon(t *testing.T) {
	e := newEnv(t)
	e.startAgent(func(c *Config) { c.AllowPrivilegedDocker = true })
	session := e.session()
	start := e.podCommand(&hostproto.PodWorkload{Command: []string{"docker", "info", "--format", "daemon {{.ServerVersion}}"}})
	start.GetStart().Image = testDockerImage
	start.GetStart().Docker = true
	start.GetStart().Source = nil
	id := start.GetStart().GetContainerId()
	session.send(t, start)
	exit := session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED).GetExit()
	if exit.GetReason() != hostproto.ExitReason_EXIT_REASON_EXITED || exit.GetExitCode() != 0 {
		t.Fatalf("docker pod exit %v; log %q", exit, e.server.containerLog(id))
	}
	waitLog(t, e.server, id, "daemon 28.5.1")
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
	ip, err := exec.CommandContext(t.Context(), "docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", id).Output()
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

// A snapshot waits for its readiness probe through the port tunnel, then
// reports unsupported with the runtime's reason on runc without CRIU, and
// the container keeps running. A probe that never answers fails the
// snapshot instead.
func TestSnapshotOnRuncWithoutCRIUIsUnsupported(t *testing.T) {
	if testRuntime() != "runc" {
		t.Skip("this runtime checkpoints")
	}
	e := newEnv(t)
	e.startAgent()
	session := e.session()
	start := e.podCommand(&hostproto.PodWorkload{Command: []string{"sh", "-c", "sleep 1; exec python3 -m http.server 8000"}})
	id := start.GetStart().GetContainerId()
	session.send(t, start)
	session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	uploads := make(chan struct{}, 2)
	store := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { uploads <- struct{}{} }))
	t.Cleanup(store.Close)
	snapshot := func(probe *hostproto.ReadinessProbe) *hostproto.CompleteSnapshotRequest {
		t.Helper()
		snapshotID := uuid.NewString()
		session.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Snapshot{Snapshot: &hostproto.SnapshotContainer{
			ContainerId: id, SnapshotId: snapshotID, UploadUrl: store.URL + "/snapshot", Deadline: timestamppb.New(time.Now().Add(time.Minute)), Ready: probe,
		}}})
		select {
		case r := <-e.server.snapshots:
			if r.GetSnapshotId() != snapshotID {
				t.Fatalf("snapshot outcome %v", r)
			}
			if _, err := os.Stat(filepath.Join(e.stateDir, "snapshots", snapshotID)); !os.IsNotExist(err) {
				t.Fatalf("the snapshot directory remains: %v", err)
			}
			return r
		case <-time.After(60 * time.Second):
			t.Fatal("no snapshot outcome")
			return nil
		}
	}
	began := time.Now()
	r := snapshot(&hostproto.ReadinessProbe{Path: "/", Port: 8000, TimeoutSeconds: 30, IntervalSeconds: 0.2})
	if !r.GetUnsupported() || !strings.Contains(strings.ToLower(r.GetFailure()), "criu") {
		t.Fatalf("snapshot outcome %v", r)
	}
	t.Logf("probe and checkpoint attempt took %s", time.Since(began))
	r = snapshot(&hostproto.ReadinessProbe{Path: "/", Port: 8001, TimeoutSeconds: 1, IntervalSeconds: 0.2})
	if r.GetUnsupported() || !strings.Contains(r.GetFailure(), "not ready") {
		t.Fatalf("snapshot of an unready container %v", r)
	}
	select {
	case <-uploads:
		t.Fatal("a failed snapshot uploaded something")
	default:
	}
	if listing := e.server.data.forward(t, portHead(id, 8000, "/"), ""); listing.status != http.StatusOK {
		t.Fatalf("the pod stopped serving after the snapshot: %+v", listing)
	}
}

// Under gVisor a running pod checkpoints once its supervisor closed its
// host sockets and keeps serving afterwards; a root agent, which reads and
// places Docker's checkpoints, uploads and restores it.
func TestSnapshotUnderRunscDetachesAndKeepsServing(t *testing.T) {
	if testRuntime() != "runsc" {
		t.Skip("needs LAZYCLOUD_TEST_OCI_RUNTIME=runsc")
	}
	e := newEnv(t)
	e.startAgent()
	session := e.session()
	start := e.podCommand(&hostproto.PodWorkload{Command: []string{"python3", "-m", "http.server", "8000"}, Ports: []int32{8000}})
	id := start.GetStart().GetContainerId()
	session.send(t, start)
	session.phase(t, id, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	var stored atomic.Pointer[[]byte]
	store := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPut {
			body, _ := io.ReadAll(r.Body)
			stored.Store(&body)
			return
		}
		if body := stored.Load(); body != nil {
			_, _ = w.Write(*body)
		}
	}))
	t.Cleanup(store.Close)

	snapshotID := uuid.NewString()
	began := time.Now()
	session.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Snapshot{Snapshot: &hostproto.SnapshotContainer{
		ContainerId: id, SnapshotId: snapshotID, UploadUrl: store.URL, Deadline: timestamppb.New(time.Now().Add(time.Minute)),
		Ready: &hostproto.ReadinessProbe{Path: "/", Port: 8000, TimeoutSeconds: 30, IntervalSeconds: 0.2},
	}}})
	var r *hostproto.CompleteSnapshotRequest
	select {
	case r = <-e.server.snapshots:
	case <-time.After(time.Minute):
		t.Fatal("no snapshot outcome")
	}
	root := os.Geteuid() == 0
	if !root {
		// The checkpoint was taken; only root reads what Docker wrote.
		if !r.GetUnsupported() || !strings.Contains(r.GetFailure(), "agent running as root") {
			t.Fatalf("snapshot outcome without root %v", r)
		}
	} else {
		body := stored.Load()
		if r.GetFailure() != "" || body == nil || int64(len(*body)) != r.GetSizeBytes() {
			t.Fatalf("snapshot outcome %v", r)
		}
		sum := sha256.Sum256(*body)
		if hex.EncodeToString(sum[:]) != r.GetSha256() {
			t.Fatal("the uploaded snapshot does not match its digest")
		}
		t.Logf("checkpoint of %d bytes uploaded in %s", r.GetSizeBytes(), time.Since(began))
	}

	// The supervisor reopens its sockets once the link is back.
	for deadline := time.Now().Add(15 * time.Second); ; time.Sleep(200 * time.Millisecond) {
		files := e.server.data.forward(t, controlHead(id, http.MethodGet, "/files?path=/workspace"), "")
		port := e.server.data.forward(t, portHead(id, 8000, "/"), "")
		if files.status == http.StatusOK && port.status == http.StatusOK {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("after the snapshot: control %+v, port %+v", files, port)
		}
	}
	t.Logf("serving again %s after the snapshot began", time.Since(began))

	if !root {
		return
	}
	restored := e.podCommand(&hostproto.PodWorkload{Command: []string{"python3", "-m", "http.server", "8000"}, Ports: []int32{8000}})
	restored.GetStart().Restore = &hostproto.SnapshotRestore{SnapshotId: snapshotID, Url: store.URL, Sha256: r.GetSha256()}
	restoredID := restored.GetStart().GetContainerId()
	began = time.Now()
	session.send(t, restored)
	ready := session.phase(t, restoredID, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	if ready.GetRestoreFailed() != "" {
		t.Fatalf("restore report %v", ready)
	}
	for deadline := time.Now().Add(15 * time.Second); ; time.Sleep(200 * time.Millisecond) {
		if port := e.server.data.forward(t, portHead(restoredID, 8000, "/"), ""); port.status == http.StatusOK {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the restored pod does not serve")
		}
	}
	t.Logf("restored and serving in %s", time.Since(began))
}

// An automatic snapshot that does not restore starts the container cold and
// says so; a requested one fails the start.
func TestFailedRestoreStartsColdOnlyForAutomaticSnapshots(t *testing.T) {
	e := newEnv(t)
	e.startAgent()
	session := e.session()
	archive := tarOf(t, map[string]string{"inventory.img": "not a checkpoint"})
	sum := sha256.Sum256(archive)
	var downloads atomic.Int32
	store := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		downloads.Add(1)
		_, _ = w.Write(archive)
	}))
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

	// Restored processes would run before a network filter applies, so a
	// policed container never fetches its snapshot.
	fetched := downloads.Load()
	policed := func(automatic bool) *hostproto.ServerMessage {
		start := e.podCommand(&hostproto.PodWorkload{Command: []string{"sleep", "600"}, Network: &hostproto.NetworkPolicy{Block: true}})
		start.GetStart().Restore = restore(automatic)
		session.send(t, start)
		return start
	}
	cold := policed(true)
	ready = session.phase(t, cold.GetStart().GetContainerId(), hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	if ready.GetRestoreFailed() != cold.GetStart().GetRestore().GetSnapshotId() {
		t.Fatalf("policed automatic restore report %v", ready)
	}
	refused := policed(false)
	exit = session.phase(t, refused.GetStart().GetContainerId(), hostproto.ContainerPhase_CONTAINER_PHASE_EXITED).GetExit()
	if exit.GetReason() != hostproto.ExitReason_EXIT_REASON_START_FAILED || !strings.Contains(exit.GetMessage(), "network policy") {
		t.Fatalf("policed requested restore exit %v", exit)
	}
	if n := downloads.Load(); n != fetched {
		t.Fatalf("policed containers fetched %d snapshots", n-fetched)
	}
}

// A pod's filesystem becomes an image in the registry with the source
// image's configuration, and the local copy is gone afterwards.
func TestPublishFilesystemPushesAnImage(t *testing.T) {
	registry := startTestRegistry(t)
	e := newEnv(t)
	e.startAgent()
	session := e.session()
	start := e.podCommand(&hostproto.PodWorkload{Command: []string{"sh", "-c", "echo published > /tmp/proof && exec sleep 600"}})
	start.GetStart().Image = readableImage(t, e.id)
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
	out, err := exec.CommandContext(t.Context(), "docker", "run", "--rm", "--label", "lazycloud.agent="+e.id, result.GetReference(), "cat", "/tmp/proof").Output()
	if err != nil || strings.TrimSpace(string(out)) != "published" {
		t.Fatalf("run the published image: %v: %s", err, out)
	}
	config, err := exec.CommandContext(t.Context(), "docker", "image", "inspect", "-f", "{{json .Config.Cmd}} {{.Config.WorkingDir}}", result.GetReference()).Output()
	if err != nil || !strings.Contains(string(config), `["sh"] /srv`) {
		t.Fatalf("published image config %s: %v", config, err)
	}
}

// readableImage builds a busybox image whose whole filesystem the test's
// unprivileged pod user can read, as an archive of / needs, with its own
// working directory to carry over.
func readableImage(t *testing.T, label string) string {
	t.Helper()
	tag := "lazycloud-agent-test-fs:" + uuid.NewString()[:8]
	build := exec.CommandContext(t.Context(), "docker", "build", "-q", "--label", "lazycloud.agent="+label, "-t", tag, "-")
	build.Stdin = strings.NewReader("FROM " + testBuildBase + "\nRUN chmod a+r /etc/shadow && chmod a+rx /root && mkdir -p /srv\nWORKDIR /srv\n")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build test image: %v: %s", err, out)
	}
	t.Cleanup(func() { _ = exec.CommandContext(context.Background(), "docker", "rmi", "-f", tag).Run() })
	return tag
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
