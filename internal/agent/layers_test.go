package agent

import (
	"context"
	"log/slog"
	"net"
	"path/filepath"
	"slices"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// snapshotterServer is the snapshotter's side of LayerSources: it records
// every grant and refuses them while refuse is set.
type snapshotterServer struct {
	imagefsproto.UnimplementedLayerSourcesServer
	mu     sync.Mutex
	grants []*imagefsproto.LayerGrant
	refuse bool
	// prefetches and traces are the Prefetch and StartTrace calls held;
	// EndTrace answers with traced.
	prefetches []*imagefsproto.PrefetchRequest
	traces     map[string][]string
	traced     []*imagefsproto.FrameRead
	stopped    []string
}

func (s *snapshotterServer) Prefetch(_ context.Context, req *imagefsproto.PrefetchRequest) (*imagefsproto.PrefetchResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.prefetches = append(s.prefetches, req)
	return &imagefsproto.PrefetchResponse{}, nil
}

func (s *snapshotterServer) StopPrefetch(_ context.Context, req *imagefsproto.StopPrefetchRequest) (*imagefsproto.StopPrefetchResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.stopped = append(s.stopped, req.GetName())
	return &imagefsproto.StopPrefetchResponse{}, nil
}

func (s *snapshotterServer) prefetchStopped(name string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return slices.Contains(s.stopped, name)
}

func (s *snapshotterServer) StartTrace(_ context.Context, req *imagefsproto.StartTraceRequest) (*imagefsproto.StartTraceResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.traces == nil {
		s.traces = map[string][]string{}
	}
	s.traces[req.GetName()] = req.GetLayers()
	return &imagefsproto.StartTraceResponse{}, nil
}

func (s *snapshotterServer) EndTrace(_ context.Context, req *imagefsproto.EndTraceRequest) (*imagefsproto.EndTraceResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, ok := s.traces[req.GetName()]; !ok {
		return nil, status.Error(codes.NotFound, "no trace")
	}
	delete(s.traces, req.GetName())
	return &imagefsproto.EndTraceResponse{Reads: s.traced, Complete: true}, nil
}

func (s *snapshotterServer) Grant(_ context.Context, req *imagefsproto.GrantRequest) (*imagefsproto.GrantResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.refuse {
		return nil, status.Error(codes.Unavailable, "refused")
	}
	s.grants = append(s.grants, req.GetLayers()...)
	return &imagefsproto.GrantResponse{}, nil
}

// has reports whether the snapshotter holds a grant of diffID expiring at.
func (s *snapshotterServer) has(diffID string, at time.Time) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, g := range s.grants {
		if g.GetDiffId() == diffID && g.GetExpiresAt().AsTime().Equal(at) {
			return true
		}
	}
	return false
}

func serveSnapshotter(t *testing.T, socket string) *snapshotterServer {
	t.Helper()
	lis, err := (&net.ListenConfig{}).Listen(t.Context(), "unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	s := &snapshotterServer{}
	g := grpc.NewServer()
	imagefsproto.RegisterLayerSourcesServer(g, s)
	go func() { _ = g.Serve(lis) }()
	t.Cleanup(g.Stop)
	return s
}

func layerGrant(diffID string, at time.Time) *hostproto.LayerGrant {
	return &hostproto.LayerGrant{
		DiffId: diffID, IndexUrl: "http://store/" + diffID + "/index", DataUrl: "http://store/" + diffID + "/data",
		ExpiresAt: timestamppb.New(at),
	}
}

// TestLayerGrantsReachTheSnapshotterBeforeThePull: the snapshotter holds a
// start's layer grants before the image is pulled, refreshes and re-sent
// starts replace them, a refused refresh is retried, and a start whose
// grants the snapshotter refuses never starts.
func TestLayerGrantsReachTheSnapshotterBeforeThePull(t *testing.T) {
	e := newEnv(t)
	socket := filepath.Join(e.stateDir, "snap.sock")
	snap := serveSnapshotter(t, socket)
	e.startAgent(func(c *Config) { c.Snapshotter = socket })
	s := e.session()

	diffID := "sha256:" + uuid.NewString()
	first := time.Now().Add(time.Hour).Truncate(time.Second)
	start := e.startCommand("app:handle", 1)
	start.GetStart().Layers = []*hostproto.LayerGrant{layerGrant(diffID, first)}
	s.send(t, start)
	s.phase(t, start.GetStart().GetContainerId(), starting)
	if !snap.has(diffID, first) {
		t.Fatal("the container started before the snapshotter held its layer grants")
	}

	held := func(at time.Time, what string) {
		t.Helper()
		deadline := time.Now().Add(10 * time.Second)
		for !snap.has(diffID, at) {
			if time.Now().After(deadline) {
				t.Fatalf("%s did not reach the snapshotter", what)
			}
			time.Sleep(20 * time.Millisecond)
		}
	}
	refuse := func(on bool) {
		snap.mu.Lock()
		snap.refuse = on
		snap.mu.Unlock()
	}

	// A refresh the snapshotter refuses is retried until it holds it.
	refuse(true)
	later := first.Add(30 * time.Minute)
	s.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_LayerGrants{
		LayerGrants: &hostproto.LayerGrants{Layers: []*hostproto.LayerGrant{layerGrant(diffID, later)}},
	}})
	time.Sleep(500 * time.Millisecond)
	refuse(false)
	held(later, "the refused refresh")

	// A start sent again for the running container carries fresh grants.
	again := proto.Clone(start).(*hostproto.ServerMessage)
	latest := later.Add(30 * time.Minute)
	again.GetStart().Layers = []*hostproto.LayerGrant{layerGrant(diffID, latest)}
	s.send(t, again)
	held(latest, "the re-sent start's grant")

	refuse(true)
	before := e.containers()
	refused := e.startCommand("app:handle", 1)
	refused.GetStart().Layers = []*hostproto.LayerGrant{layerGrant("sha256:"+uuid.NewString(), first)}
	s.send(t, refused)
	report := s.phase(t, refused.GetStart().GetContainerId(), exited)
	if report.GetExit().GetReason() != hostproto.ExitReason_EXIT_REASON_START_FAILED {
		t.Fatalf("refused grants ended the start as %v", report.GetExit())
	}
	if e.containers() != before {
		t.Fatal("a start whose grants were refused created a container")
	}
}

// TestStartupTracesEndWithTheFirstTask: a start's prefetch reaches the
// snapshotter before the container runs, and a start that records its trace
// keeps it running once ready, so a handler's imports are traced, and
// reports what the snapshotter traced when its first task ends.
func TestStartupTracesEndWithTheFirstTask(t *testing.T) {
	e := newEnv(t)
	socket := filepath.Join(e.stateDir, "snap.sock")
	snap := serveSnapshotter(t, socket)
	snap.traced = []*imagefsproto.FrameRead{{Layer: 0, Frame: 7}, {Layer: 0, Frame: 2}}
	e.startAgent(func(c *Config) { c.Snapshotter = socket })
	s := e.session()

	diffID := "sha256:" + uuid.NewString()
	start := e.startCommand("app:handle", 1)
	id := start.GetStart().GetContainerId()
	start.GetStart().Layers = []*hostproto.LayerGrant{layerGrant(diffID, time.Now().Add(time.Hour))}
	start.GetStart().Prefetch = &hostproto.ImageTrace{Reads: []*hostproto.FrameRead{{Layer: 0, Frame: 3}}}
	start.GetStart().RecordTrace = true
	s.send(t, start)
	s.phase(t, id, starting)
	snap.mu.Lock()
	prefetched := len(snap.prefetches) == 1 && slices.Equal(snap.prefetches[0].GetLayers(), []string{diffID}) &&
		snap.prefetches[0].GetReads()[0].GetFrame() == 3
	tracing := slices.Equal(snap.traces[id], []string{diffID})
	snap.mu.Unlock()
	if !prefetched || !tracing {
		t.Fatalf("before the container ran the snapshotter had the prefetch %v and the trace %v", prefetched, tracing)
	}
	s.phase(t, id, ready)
	time.Sleep(500 * time.Millisecond)
	snap.mu.Lock()
	_, tracing = snap.traces[id]
	snap.mu.Unlock()
	if !tracing {
		t.Fatal("the trace ended when the container was ready, before its first task")
	}
	e.completion(e.task(id, `{"args": ["total", [1, 2]]}`))
	reported := s.until(t, 10*time.Second, func(m *hostproto.HostMessage) bool { return m.GetStartupTrace() != nil }).GetStartupTrace()
	if reported.GetContainerId() != id || len(reported.GetTrace().GetReads()) != 2 || reported.GetTrace().GetReads()[0].GetFrame() != 7 {
		t.Fatalf("the host reported %v", reported)
	}
}

// startingWith is a start of a container recording its trace, with grants
// for layers.
func startingWith(layers ...string) *hostproto.StartContainer {
	spec := &hostproto.StartContainer{RecordTrace: true, Prefetch: &hostproto.ImageTrace{Reads: []*hostproto.FrameRead{{}}}}
	for _, l := range layers {
		spec.Layers = append(spec.Layers, layerGrant(l, time.Now().Add(time.Hour)))
	}
	return spec
}

// TestSharedStartsReportNoTrace: the snapshotter cannot tell apart the
// reads of two starts sharing a layer, so neither reports a trace, and a
// start that ends stops its prefetch and trace; a start alone on its
// layers reports its trace.
func TestSharedStartsReportNoTrace(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "snap.sock")
	snap := serveSnapshotter(t, socket)
	snap.traced = []*imagefsproto.FrameRead{{Layer: 0, Frame: 1}}
	client, err := layersource.Dial(socket)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	l := newLayerSources(client)
	log := slog.New(slog.DiscardHandler)
	digest := func() string { return "sha256:" + uuid.NewString() }
	base := digest()

	for _, c := range []string{"c1", "c2"} {
		if _, err := l.start(t.Context(), log, c, startingWith(base, digest())); err != nil {
			t.Fatal(err)
		}
	}
	for _, c := range []string{"c1", "c2"} {
		if trace := l.end(t.Context(), log, c); trace != nil {
			t.Fatalf("%s reported a trace though another start shared its base layer", c)
		}
	}
	l.release(t.Context(), log, "c1")
	snap.mu.Lock()
	_, tracing := snap.traces["c1"]
	snap.mu.Unlock()
	if !snap.prefetchStopped("c1") || tracing {
		t.Fatal("an ended start left its prefetch or trace running")
	}

	if _, err := l.start(t.Context(), log, "alone", startingWith(digest())); err != nil {
		t.Fatal(err)
	}
	if trace := l.end(t.Context(), log, "alone"); len(trace.GetReads()) != 1 {
		t.Fatalf("a start alone on its layers reported %v", trace)
	}
}

// TestStartupTracesEndWithinTheWindow: a container that serves nothing
// ends its trace and reports it a window after the trace began, and one
// that exits first ends the wait at once and reports none.
func TestStartupTracesEndWithinTheWindow(t *testing.T) {
	socket := filepath.Join(t.TempDir(), "snap.sock")
	snap := serveSnapshotter(t, socket)
	snap.traced = []*imagefsproto.FrameRead{{Layer: 0, Frame: 1}}
	client, err := layersource.Dial(socket)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	l := newLayerSources(client)
	log := slog.New(slog.DiscardHandler)
	const window = 300 * time.Millisecond

	began := time.Now()
	if tracing, err := l.start(t.Context(), log, "idle", startingWith("sha256:"+uuid.NewString())); err != nil || !tracing {
		t.Fatalf("the start traces %v: %v", tracing, err)
	}
	trace := l.await(t.Context(), log, "idle", window)
	if waited := time.Since(began); len(trace.GetReads()) != 1 || waited < window || waited > window+5*time.Second {
		t.Fatalf("an idle container reported %v after %s of a %s window", trace, waited, window)
	}

	if _, err := l.start(t.Context(), log, "exits", startingWith("sha256:"+uuid.NewString())); err != nil {
		t.Fatal(err)
	}
	time.AfterFunc(50*time.Millisecond, func() { l.release(context.Background(), log, "exits") })
	began = time.Now()
	if trace := l.await(t.Context(), log, "exits", time.Hour); trace != nil || time.Since(began) > 5*time.Second {
		t.Fatalf("an exited container reported %v after %s", trace, time.Since(began))
	}
}

// TestFailedStartsStopTheirPrefetch: a start that fails after its prefetch
// began stops it, so failed starts never hold the snapshotter's prefetches.
func TestFailedStartsStopTheirPrefetch(t *testing.T) {
	e := newEnv(t)
	socket := filepath.Join(e.stateDir, "snap.sock")
	snap := serveSnapshotter(t, socket)
	e.startAgent(func(c *Config) { c.Snapshotter = socket })
	s := e.session()

	start := e.startCommand("app:handle", 1)
	id := start.GetStart().GetContainerId()
	start.GetStart().Image = "127.0.0.1:1/missing/image:latest"
	start.GetStart().Layers = []*hostproto.LayerGrant{layerGrant("sha256:"+uuid.NewString(), time.Now().Add(time.Hour))}
	start.GetStart().Prefetch = &hostproto.ImageTrace{Reads: []*hostproto.FrameRead{{}}}
	s.send(t, start)
	if report := s.phase(t, id, exited); report.GetExit().GetReason() != hostproto.ExitReason_EXIT_REASON_START_FAILED {
		t.Fatalf("the start ended %v", report.GetExit())
	}
	for deadline := time.Now().Add(10 * time.Second); !snap.prefetchStopped(id); time.Sleep(20 * time.Millisecond) {
		if time.Now().After(deadline) {
			t.Fatal("a failed start left its prefetch running")
		}
	}
}
