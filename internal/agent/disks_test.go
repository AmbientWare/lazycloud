//go:build hostruntime

package agent

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// TestDiskLeasesReleaseUntilAccepted covers a lease left by a container that
// never attached its disk: the release loop releases it without publishing,
// keeps the record when the server fails and records the failure on the
// disk, and retries after an agent restart until the server accepts.
func TestDiskLeasesReleaseUntilAccepted(t *testing.T) {
	t.Parallel()
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
