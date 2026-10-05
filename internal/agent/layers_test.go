package agent

import (
	"context"
	"net"
	"path/filepath"
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
)

// snapshotterServer is the snapshotter's side of LayerSources: it records
// every grant and refuses them while refuse is set.
type snapshotterServer struct {
	imagefsproto.UnimplementedLayerSourcesServer
	mu     sync.Mutex
	grants []*imagefsproto.LayerGrant
	refuse bool
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
