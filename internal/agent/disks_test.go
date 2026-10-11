package agent

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

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

// TestDiskLeasesReleaseUntilAccepted covers a lease left by a container that
// never attached its disk: the release loop releases it without publishing,
// keeps the record when the server fails and records the failure on the
// disk, and retries after an agent restart until the server accepts.
func TestDiskLeasesReleaseUntilAccepted(t *testing.T) {
	e := newEnv(t)
	e.server.releases = &releases{failures: 1}
	container, disk := uuid.NewString(), uuid.NewString()
	leases := filepath.Join(e.stateDir, "disks", "leases")
	if err := os.MkdirAll(leases, 0o700); err != nil {
		t.Fatal(err)
	}
	record, err := json.Marshal([]*heldDisk{{ID: disk, Name: "root", Workspace: uuid.NewString(), Token: []byte("token")}})
	if err != nil {
		t.Fatal(err)
	}
	file := filepath.Join(leases, container+".json")
	if err := os.WriteFile(file, record, 0o600); err != nil {
		t.Fatal(err)
	}

	first := e.startAgent()
	deadline := time.Now().Add(10 * time.Second)
	for e.server.releases.failuresLeft() > 0 && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	for len(e.server.releases.failuresReported()) == 0 && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	first.stop()
	if _, err := os.Stat(file); err != nil {
		t.Fatalf("a refused release dropped its lease record: %v", err)
	}
	reported := e.server.releases.failuresReported()
	if len(reported) != 1 || reported[0].GetOperation() != hostproto.DiskOperation_DISK_OPERATION_RELEASE ||
		!strings.Contains(reported[0].GetMessage(), "release refused for the test") {
		t.Fatalf("reported failures %v, want the refused release", reported)
	}

	e.startAgent()
	for e.server.releases.count() == 0 && time.Now().Before(deadline.Add(10*time.Second)) {
		time.Sleep(50 * time.Millisecond)
	}
	if e.server.releases.count() != 1 {
		t.Fatalf("released %d times, want once", e.server.releases.count())
	}
	got := e.server.releases.released[0]
	if got.GetContainerId() != container || got.GetDiskId() != disk || string(got.GetLeaseToken()) != "token" {
		t.Fatalf("release %v", got)
	}
	for time.Now().Before(deadline.Add(10 * time.Second)) {
		if _, err := os.Stat(file); errors.Is(err, os.ErrNotExist) {
			return
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("the lease record outlived its release")
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
