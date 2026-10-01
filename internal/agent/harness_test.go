package agent

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io/fs"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	cerrdefs "github.com/containerd/errdefs"
	"github.com/google/uuid"
	"github.com/moby/moby/client"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const testImage = "python:3.12-slim"

// supervisorBinary is built once per test binary by TestMain.
var supervisorBinary string //nolint:gochecknoglobals // built once in TestMain

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "lcsupbin")
	if err != nil {
		panic(err)
	}
	supervisorBinary = filepath.Join(dir, "supervisor")
	build := exec.CommandContext(context.Background(), "go", "build", "-o", supervisorBinary, "../../cmd/supervisor")
	build.Env = append(os.Environ(), "CGO_ENABLED=0")
	build.Stdout, build.Stderr = os.Stdout, os.Stderr
	if err := build.Run(); err != nil {
		panic(fmt.Sprintf("build supervisor: %v", err))
	}
	code := m.Run()
	_ = os.RemoveAll(dir)
	os.Exit(code)
}

// hostServer is the server's side of HostService for focused agent tests.
// Integration with the real server happens in the integrated slice.
type hostServer struct {
	hostproto.UnimplementedHostServiceServer
	joinToken, hostID, hostToken string

	sessions    chan *serverSession
	enrolls     chan *hostproto.EnrollRequest
	completions chan *hostproto.CompleteTaskRequest
	builds      chan *hostproto.CompleteImageBuildRequest
	snapshots   chan *hostproto.CompleteSnapshotRequest
	filesystems chan *hostproto.CompleteFilesystemImageRequest
	data        *dataServer

	mu      sync.Mutex
	queued  map[string][]*hostproto.ClaimedTask
	changed chan struct{}
	logs    []*hostproto.LogLine
	// containerLogs holds each container's logged lines.
	containerLogs map[string][]string
	// buildLogs holds image build output in arrival order.
	buildLogs []string
	// appendDelay makes AppendLogs a slow consumer.
	appendDelay time.Duration
	// releases answers ReleaseDisk in disk tests.
	releases *releases
	// completeOutage fails CompleteTask as unavailable until it passes.
	completeOutage time.Time
}

type serverSession struct {
	hello  *hostproto.Hello
	stream hostproto.HostService_SessionServer
	msgs   chan *hostproto.HostMessage
}

func newHostServer() *hostServer {
	return &hostServer{
		joinToken: "lc_join", hostID: uuid.NewString(), hostToken: "lc_host_" + uuid.NewString(),
		sessions:      make(chan *serverSession, 8),
		enrolls:       make(chan *hostproto.EnrollRequest, 8),
		completions:   make(chan *hostproto.CompleteTaskRequest, 64),
		builds:        make(chan *hostproto.CompleteImageBuildRequest, 8),
		snapshots:     make(chan *hostproto.CompleteSnapshotRequest, 8),
		filesystems:   make(chan *hostproto.CompleteFilesystemImageRequest, 8),
		data:          &dataServer{calls: make(chan *forwardCall, 64)},
		queued:        map[string][]*hostproto.ClaimedTask{},
		containerLogs: map[string][]string{},
		changed:       make(chan struct{}),
	}
}

func (s *hostServer) authorize(ctx context.Context, method string) error {
	if method == hostproto.HostService_Enroll_FullMethodName {
		return nil
	}
	md, _ := metadata.FromIncomingContext(ctx)
	s.mu.Lock()
	token := s.hostToken
	s.mu.Unlock()
	if got := md.Get("authorization"); len(got) != 1 || got[0] != "Bearer "+token {
		return status.Error(codes.Unauthenticated, "bad host token")
	}
	return nil
}

func (s *hostServer) serve(t *testing.T, address string) (*grpc.Server, string) {
	t.Helper()
	listener, err := (&net.ListenConfig{}).Listen(t.Context(), "tcp", address)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(
		grpc.MaxRecvMsgSize(maxMessageBytes),
		grpc.UnaryInterceptor(func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
			if err := s.authorize(ctx, info.FullMethod); err != nil {
				return nil, err
			}
			return handler(ctx, req)
		}),
		grpc.StreamInterceptor(func(srv any, ss grpc.ServerStream, info *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
			if err := s.authorize(ss.Context(), info.FullMethod); err != nil {
				return err
			}
			return handler(srv, ss)
		}),
	)
	hostproto.RegisterHostServiceServer(server, s)
	hostproto.RegisterHostDataServer(server, s.data)
	go func() { _ = server.Serve(listener) }()
	return server, listener.Addr().String()
}

func (s *hostServer) Enroll(_ context.Context, r *hostproto.EnrollRequest) (*hostproto.EnrollResponse, error) {
	s.enrolls <- r
	if r.GetJoinToken() != s.joinToken && r.GetCloudIdentity() == nil {
		return nil, status.Error(codes.PermissionDenied, "bad join token")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return &hostproto.EnrollResponse{HostId: s.hostID, HostToken: s.hostToken}, nil
}

// revoke replaces the host token, as removing the machine does.
func (s *hostServer) revoke() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.hostToken = "lc_host_" + uuid.NewString()
}

func (s *hostServer) Session(stream hostproto.HostService_SessionServer) error {
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	session := &serverSession{hello: first.GetHello(), stream: stream, msgs: make(chan *hostproto.HostMessage, 1024)}
	s.sessions <- session
	for {
		m, err := stream.Recv()
		if err != nil {
			close(session.msgs)
			return err
		}
		session.msgs <- m
	}
}

func (s *hostServer) enqueue(container string, task *hostproto.ClaimedTask) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.queued[container] = append(s.queued[container], task)
	close(s.changed)
	s.changed = make(chan struct{})
}

func (s *hostServer) ClaimTasks(ctx context.Context, r *hostproto.ClaimTasksRequest) (*hostproto.ClaimTasksResponse, error) {
	deadline := time.After(time.Duration(r.GetWaitSeconds()) * time.Second)
	for {
		s.mu.Lock()
		queue := s.queued[r.GetContainerId()]
		n := min(len(queue), int(r.GetMaxTasks()))
		if n > 0 {
			s.queued[r.GetContainerId()] = queue[n:]
			s.mu.Unlock()
			return &hostproto.ClaimTasksResponse{Tasks: queue[:n]}, nil
		}
		changed := s.changed
		s.mu.Unlock()
		select {
		case <-changed:
		case <-deadline:
			return &hostproto.ClaimTasksResponse{}, nil
		case <-ctx.Done():
			return nil, status.FromContextError(ctx.Err()).Err()
		}
	}
}

func (s *hostServer) CompleteTask(_ context.Context, r *hostproto.CompleteTaskRequest) (*hostproto.CompleteTaskResponse, error) {
	s.mu.Lock()
	outage := time.Now().Before(s.completeOutage)
	s.mu.Unlock()
	if outage {
		return nil, status.Error(codes.Unavailable, "server is restarting")
	}
	s.completions <- r
	return &hostproto.CompleteTaskResponse{}, nil
}

func (s *hostServer) AppendLogs(_ context.Context, r *hostproto.AppendLogsRequest) (*hostproto.AppendLogsResponse, error) {
	s.mu.Lock()
	delay := s.appendDelay
	s.mu.Unlock()
	time.Sleep(delay)
	s.mu.Lock()
	defer s.mu.Unlock()
	s.logs = append(s.logs, r.GetLines()...)
	for _, line := range r.GetLines() {
		s.containerLogs[r.GetContainerId()] = append(s.containerLogs[r.GetContainerId()], line.GetData())
	}
	return &hostproto.AppendLogsResponse{}, nil
}

// output joins the logged lines of one attempt.
func (s *hostServer) output(attempt string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var b strings.Builder
	for _, line := range s.logs {
		if line.GetAttemptId() == attempt {
			b.WriteString(line.GetData() + "\n")
		}
	}
	return b.String()
}

// env is one test's server, source store, state directory and agents.
type env struct {
	t        *testing.T
	id       string
	server   *hostServer
	grpc     *grpc.Server
	address  string
	stateDir string
	docker   *client.Client
	source   *hostproto.Source
	// metricsInterval overrides the agent's sampling interval.
	metricsInterval time.Duration
	geesefs         string
}

func newEnv(t *testing.T) *env {
	t.Helper()
	docker, err := client.New(client.FromEnv)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := docker.Ping(t.Context(), client.PingOptions{}); err != nil {
		t.Fatalf("docker is required for agent tests: %v", err)
	}
	e := &env{t: t, id: "agent-test-" + uuid.NewString()[:8], server: newHostServer(), docker: docker}
	e.grpc, e.address = e.server.serve(t, "127.0.0.1:0")
	// A short path keeps container sockets under the Unix socket limit.
	if e.stateDir, err = os.MkdirTemp("", "lca"); err != nil {
		t.Fatal(err)
	}
	e.source = serveSource(t, "../supervisor/testdata/workspace")
	t.Cleanup(func() {
		e.grpc.Stop()
		e.removeContainers()
		_ = os.RemoveAll(e.stateDir)
		_ = docker.Close()
	})
	return e
}

// removeContainers deletes every container this test created.
func (e *env) removeContainers() {
	ctx := context.Background()
	list, err := e.docker.ContainerList(ctx, client.ContainerListOptions{All: true, Filters: client.Filters{}.Add("label", "lazycloud.agent="+e.id)})
	if err != nil {
		e.t.Errorf("list test containers: %v", err)
		return
	}
	for _, c := range list.Items {
		// The agent may be removing a container it saw exit.
		if _, err := e.docker.ContainerRemove(ctx, c.ID, client.ContainerRemoveOptions{Force: true}); err != nil && !cerrdefs.IsNotFound(err) && !cerrdefs.IsConflict(err) {
			e.t.Errorf("remove test container: %v", err)
		}
	}
}

func (e *env) containers() int {
	list, err := e.docker.ContainerList(context.Background(), client.ContainerListOptions{All: true, Filters: client.Filters{}.Add("label", "lazycloud.agent="+e.id)})
	if err != nil {
		e.t.Fatal(err)
	}
	return len(list.Items)
}

type runningAgent struct {
	cancel context.CancelFunc
	done   chan error
}

// startAgent runs an agent; configure adjusts its configuration.
func (e *env) startAgent(configure ...func(*Config)) *runningAgent {
	e.t.Helper()
	runtimeDir, err := filepath.Abs("../supervisor/testdata/runtime")
	if err != nil {
		e.t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	a := &runningAgent{cancel: cancel, done: make(chan error, 1)}
	cfg := Config{
		Server:          e.address,
		ServerPlaintext: true,
		StateDir:        e.stateDir,
		SocketDir:       filepath.Join(e.stateDir, "s"),
		JoinToken:       e.server.joinToken,
		RuntimeDir:      runtimeDir,
		SupervisorPath:  supervisorBinary,
		OCIRuntime:      "runc",
		GeeseFSPath:     e.geesefs,
		MountImage:      DefaultMountImage,
		BuildNetwork:    "host",
		Limits:          Limits{CPUMillis: 4000, MemoryBytes: 8 << 30},
		Labels:          map[string]string{"lazycloud.agent": e.id},
		Version:         "test",
		MetricsInterval: e.metricsInterval,
		Logger:          slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo})),
	}
	for _, fn := range configure {
		fn(&cfg)
	}
	go func() { a.done <- Run(ctx, cfg) }()
	e.t.Cleanup(a.stop)
	return a
}

func (a *runningAgent) stop() {
	a.cancel()
	select {
	case <-a.done:
	case <-time.After(30 * time.Second):
		panic("agent did not stop")
	}
	a.done <- nil
}

// exited waits for Run to return on its own.
func (a *runningAgent) exited(t *testing.T) error {
	t.Helper()
	select {
	case err := <-a.done:
		a.done <- nil
		return err
	case <-time.After(60 * time.Second):
		t.Fatal("agent did not exit")
		return nil
	}
}

func (e *env) enrollment() *hostproto.EnrollRequest {
	e.t.Helper()
	select {
	case r := <-e.server.enrolls:
		return r
	case <-time.After(30 * time.Second):
		e.t.Fatal("agent did not enroll")
		return nil
	}
}

func (e *env) session() *serverSession {
	e.t.Helper()
	select {
	case s := <-e.server.sessions:
		return s
	case <-time.After(30 * time.Second):
		e.t.Fatal("agent did not open a session")
		return nil
	}
}

func (s *serverSession) send(t *testing.T, m *hostproto.ServerMessage) {
	t.Helper()
	if err := s.stream.Send(m); err != nil {
		t.Fatal(err)
	}
}

// until reads host messages until match accepts one.
func (s *serverSession) until(t *testing.T, timeout time.Duration, match func(*hostproto.HostMessage) bool) *hostproto.HostMessage {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case m, ok := <-s.msgs:
			if !ok {
				t.Fatal("session closed")
			}
			if match(m) {
				return m
			}
		case <-deadline:
			t.Fatal("timed out waiting for a host message")
			return nil
		}
	}
}

func (s *serverSession) phase(t *testing.T, container string, phase hostproto.ContainerPhase) *hostproto.ContainerReport {
	t.Helper()
	return s.until(t, 120*time.Second, func(m *hostproto.HostMessage) bool {
		r := m.GetContainer()
		return r.GetContainerId() == container && r.GetPhase() == phase
	}).GetContainer()
}

func (e *env) startCommand(handler string, slots int32) *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Start{Start: &hostproto.StartContainer{
		ContainerId:   uuid.NewString(),
		Image:         testImage,
		PythonVersion: "3.12",
		Source:        e.source,
		Resources:     &hostproto.Resources{CpuMillis: 1000, MemoryBytes: 256 << 20},
		Function:      &hostproto.FunctionWorkload{Handler: handler, Slots: slots},
		// The agent runs unprivileged in tests; root-owned bytecode in the
		// workspace would block its cleanup.
		Environment: map[string]string{"PYTHONDONTWRITEBYTECODE": "1"},
	}}}
}

func stopCommand(container string, grace int32) *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Stop{Stop: &hostproto.StopContainer{
		ContainerId: container, GraceSeconds: grace,
	}}}
}

func cancelCommand(container, attempt string) *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Cancel{Cancel: &hostproto.CancelAttempt{
		ContainerId: container, AttemptId: attempt, Reason: hostproto.CancelReason_CANCEL_REASON_CANCELLED,
	}}}
}

// task queues a JSON task and returns its attempt id.
func (e *env) task(container, input string) string {
	attempt := uuid.NewString()
	e.server.enqueue(container, &hostproto.ClaimedTask{
		TaskId: uuid.NewString(), AttemptId: attempt, AttemptNumber: 1,
		InputEncoding: hostproto.PayloadEncoding_PAYLOAD_ENCODING_JSON, Input: []byte(input),
	})
	return attempt
}

func (e *env) completion(attempt string) *hostproto.CompleteTaskRequest {
	e.t.Helper()
	deadline := time.After(60 * time.Second)
	for {
		select {
		case r := <-e.server.completions:
			if r.GetAttemptId() == attempt {
				return r
			}
			e.t.Fatalf("unexpected completion for %s while waiting for %s: %v", r.GetAttemptId(), attempt, r)
		case <-deadline:
			e.t.Fatalf("no completion for %s", attempt)
			return nil
		}
	}
}

// serveSource zips dir and serves it the way a presigned URL would.
func serveSource(t *testing.T, dir string) *hostproto.Source {
	t.Helper()
	var buf bytes.Buffer
	w := zip.NewWriter(&buf)
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		name, _ := filepath.Rel(dir, path)
		f, err := w.Create(filepath.ToSlash(name))
		if err != nil {
			return err
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		_, err = f.Write(data)
		return err
	})
	if err == nil {
		err = w.Close()
	}
	if err != nil {
		t.Fatal(err)
	}
	archive := buf.Bytes()
	sum := sha256.Sum256(archive)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(archive)
	}))
	t.Cleanup(server.Close)
	return &hostproto.Source{Sha256: hex.EncodeToString(sum[:]), Url: server.URL + "/source.zip"}
}
