package agent

import (
	"context"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

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
	// completeOutage fails CompleteTask as unavailable while set;
	// refusedCompletions counts the calls it failed.
	completeOutage     bool
	refusedCompletions int
	// answerBuild answers CompleteImageBuild when set.
	answerBuild func(*hostproto.CompleteImageBuildRequest) *hostproto.CompleteImageBuildResponse
	// answerPlatform gives the platform images a session sends at open,
	// none when it returns none or is unset.
	answerPlatform func(named []string) []*hostproto.PlatformImage
}

type serverSession struct {
	hello  *hostproto.Hello
	stream hostproto.HostService_SessionServer
	msgs   chan *hostproto.HostMessage
	// end ends the session from the server's side.
	end chan struct{}
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
	session := &serverSession{hello: first.GetHello(), stream: stream, msgs: make(chan *hostproto.HostMessage, 1024), end: make(chan struct{})}
	// As the server does once it converted them, the session answers the
	// platform images the Hello names with their grants.
	s.mu.Lock()
	answer := s.answerPlatform
	s.mu.Unlock()
	var platform []*hostproto.PlatformImage
	if answer != nil {
		platform = answer(session.hello.GetPlatformImages())
	}
	if len(platform) > 0 {
		if err := stream.Send(&hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_PlatformImages{
			PlatformImages: &hostproto.PlatformImages{Images: platform},
		}}); err != nil {
			return err
		}
	}
	s.sessions <- session
	received := make(chan error, 1)
	go func() {
		for {
			m, err := stream.Recv()
			if err != nil {
				close(session.msgs)
				received <- err
				return
			}
			session.msgs <- m
		}
	}()
	select {
	case err := <-received:
		return err
	case <-session.end:
		return status.Error(codes.Unavailable, "the test ended the session")
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
	outage := s.completeOutage
	if outage {
		s.refusedCompletions++
	}
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

// shortDir is a directory removed when the test ends, short enough to hold
// Unix sockets, which a test's own directory may not be.
func shortDir(t *testing.T) string {
	t.Helper()
	dir, err := os.MkdirTemp("", "lca")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(dir) })
	return dir
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

func (s *hostServer) CompleteSnapshot(_ context.Context, r *hostproto.CompleteSnapshotRequest) (*hostproto.CompleteSnapshotResponse, error) {
	s.snapshots <- r
	return &hostproto.CompleteSnapshotResponse{}, nil
}

func (s *hostServer) CompleteFilesystemImage(_ context.Context, r *hostproto.CompleteFilesystemImageRequest) (*hostproto.CompleteFilesystemImageResponse, error) {
	s.filesystems <- r
	return &hostproto.CompleteFilesystemImageResponse{}, nil
}

// containerLog joins the output the container logged outside attempts.
func (s *hostServer) containerLog(container string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return strings.Join(s.containerLogs[container], "\n")
}

func (s *hostServer) CompleteImageBuild(_ context.Context, r *hostproto.CompleteImageBuildRequest) (*hostproto.CompleteImageBuildResponse, error) {
	s.builds <- r
	s.mu.Lock()
	answer := s.answerBuild
	s.mu.Unlock()
	if answer != nil {
		return answer(r), nil
	}
	return &hostproto.CompleteImageBuildResponse{}, nil
}

func (s *hostServer) AppendImageBuildLogs(_ context.Context, r *hostproto.AppendImageBuildLogsRequest) (*hostproto.AppendImageBuildLogsResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, line := range r.GetLines() {
		s.buildLogs = append(s.buildLogs, line.GetData())
	}
	return &hostproto.AppendImageBuildLogsResponse{}, nil
}

func (s *hostServer) buildOutput() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return strings.Join(s.buildLogs, "\n")
}

// releases records ReleaseDisk calls and fails the first failures of them,
// and records the failures the agent reports.
type releases struct {
	mu       sync.Mutex
	failures int
	released []*hostproto.ReleaseDiskRequest
	reported []*hostproto.DiskFailure
}

func (r *releases) release(req *hostproto.ReleaseDiskRequest) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.failures > 0 {
		r.failures--
		return status.Error(codes.Unavailable, "release refused for the test")
	}
	r.released = append(r.released, req)
	return nil
}

func (r *releases) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.released)
}

func (r *releases) failuresReported() []*hostproto.DiskFailure {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]*hostproto.DiskFailure(nil), r.reported...)
}

func (s *hostServer) RecordDiskFailure(_ context.Context, req *hostproto.RecordDiskFailureRequest) (*hostproto.RecordDiskFailureResponse, error) {
	s.releases.mu.Lock()
	defer s.releases.mu.Unlock()
	if req.GetFailure() != nil {
		s.releases.reported = append(s.releases.reported, req.GetFailure())
	}
	return &hostproto.RecordDiskFailureResponse{}, nil
}

func (r *releases) failuresLeft() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.failures
}

func (s *hostServer) ReleaseDisk(_ context.Context, req *hostproto.ReleaseDiskRequest) (*hostproto.ReleaseDiskResponse, error) {
	if s.releases == nil {
		return nil, status.Error(codes.Unimplemented, "no disks in this test")
	}
	if err := s.releases.release(req); err != nil {
		return nil, err
	}
	return &hostproto.ReleaseDiskResponse{}, nil
}
