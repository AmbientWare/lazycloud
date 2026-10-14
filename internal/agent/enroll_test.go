//go:build hostruntime

package agent

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

func TestAgentEnrollsWithPreflightChecks(t *testing.T) {
	t.Parallel()
	e := newEnv(t)
	e.startAgent()
	enroll := e.enrollment()
	s := e.session()
	if enroll.GetArchitecture() != runtime.GOARCH {
		t.Fatalf("architecture %q", enroll.GetArchitecture())
	}
	checks := map[string]*hostproto.PreflightCheck{}
	for _, check := range enroll.GetPreflight() {
		checks[check.GetName()] = check
	}
	for name, message := range map[string]string{"docker": "Docker is reachable", "capacity.max_cpu": "using 2000m CPU", "capacity.max_memory": "using 8192 MB memory"} {
		if check := checks[name]; !check.GetOk() || check.GetMessage() != message {
			t.Errorf("check %s = %v, want %q", name, check, message)
		}
	}
	gpus := detectGPUs(t.Context())
	if len(gpus) > 0 && nvidiaRuntimeInstalled() {
		capacity := enroll.GetCapacity()
		if capacity.GetGpuCount() != int32(len(gpus)) || capacity.GetGpuType() != gpus[0].Model {
			t.Fatalf("enrolled with %d %q GPUs, detected %v", capacity.GetGpuCount(), capacity.GetGpuType(), gpus)
		}
		t.Logf("this host offers %d %s", capacity.GetGpuCount(), capacity.GetGpuType())
	}
	if hello := s.hello.GetCapacity(); hello.GetGpuCount() != enroll.GetCapacity().GetGpuCount() || hello.GetCpuMillis() != 2000 {
		t.Fatalf("hello capacity %v, enrolled %v", hello, enroll.GetCapacity())
	}
}

func TestAgentWithAFailedPreflightEnrollsButDoesNotJoin(t *testing.T) {
	t.Parallel()
	e := newEnv(t)
	tokenFile := filepath.Join(e.stateDir, "join-token")
	if err := os.WriteFile(tokenFile, []byte(e.server.joinToken+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	a := e.startAgent(func(cfg *Config) {
		cfg.JoinToken, cfg.JoinTokenFile = "", tokenFile
		cfg.Limits.CPUMillis = 1 << 40
	})
	enroll := e.enrollment()
	if enroll.GetJoinToken() != e.server.joinToken {
		t.Fatalf("enrolled with token %q", enroll.GetJoinToken())
	}
	failed := false
	for _, check := range enroll.GetPreflight() {
		failed = failed || (check.GetName() == "capacity.max_cpu" && !check.GetOk() && check.GetMessage() == "requested CPU exceeds detected CPU")
	}
	if !failed {
		t.Fatalf("enrollment did not report the failed CPU check: %v", enroll.GetPreflight())
	}
	if err := a.exited(t); !errors.Is(err, ErrPreflightFailed) {
		t.Fatalf("Run returned %v", err)
	}
	for _, path := range []string{tokenFile, identityPath(e.stateDir)} {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			t.Errorf("%s remains after a failed join: %v", path, err)
		}
	}
}

func TestCloudHostEnrollsAndReportsSpotInterruptions(t *testing.T) {
	t.Parallel()
	metadata := newIMDSEmulator(t)
	e := newEnv(t)
	e.startAgent(func(cfg *Config) {
		cfg.JoinToken, cfg.CloudHostID, cfg.IMDSEndpoint, cfg.InterruptionPoll = "", e.server.hostID, metadata.URL, 50*time.Millisecond
	})
	enroll := e.enrollment()
	if enroll.GetJoinToken() != "" || enroll.GetCloudIdentity().GetHostId() != e.server.hostID || enroll.GetCloudIdentity().GetUrl() == "" {
		t.Fatalf("cloud enrollment %v", enroll)
	}
	s := e.session()

	// A notice arrives after the session token expired; the agent renews it.
	metadata.expireTokens()
	reclaimAt := time.Now().Add(2 * time.Minute).Truncate(time.Second)
	metadata.setNotice("terminate", reclaimAt)
	isInterruption := func(m *hostproto.HostMessage) bool { return m.GetInterruption() != nil }
	notice := s.until(t, 20*time.Second, isInterruption).GetInterruption()
	if notice.GetReason() != "aws-ec2-spot-terminate" || !notice.GetReclaimAt().AsTime().Equal(reclaimAt) {
		t.Fatalf("interruption %v", notice)
	}
	if metadata.refusals() == 0 {
		t.Fatal("the emulator never refused an expired token")
	}

	// A new session restates the standing notice right after Hello.
	e.grpc.Stop()
	e.grpc, _ = e.server.serve(t, e.address)
	s = e.session()
	if again := s.until(t, 10*time.Second, func(*hostproto.HostMessage) bool { return true }); again.GetInterruption().GetReason() != notice.GetReason() {
		t.Fatalf("first message after reconnect %v", again)
	}
}

// TestAgentStopsWhenItsCredentialIsRevoked: a removed host's agent stops
// and leaves no identity, slice or storage key behind.
func TestAgentStopsWhenItsCredentialIsRevoked(t *testing.T) {
	t.Parallel()
	e := newEnv(t)
	t.Cleanup(e.stopMounts)
	a := e.startAgent()
	e.session()
	slice := e.plantSlice()
	keys := filepath.Join(e.stateDir, "storage", "buckets", "planted")
	if err := os.MkdirAll(keys, 0o700); err != nil {
		t.Fatal(err)
	}

	// Removing the machine replaces its token; the agent's next session is
	// refused.
	e.server.revoke()
	e.grpc.Stop()
	e.grpc, _ = e.server.serve(t, e.address)
	if err := a.exited(t); !errors.Is(err, ErrCredentialRevoked) {
		t.Fatalf("Run returned %v", err)
	}
	for _, left := range []string{identityPath(e.stateDir), slice, keys} {
		if _, err := os.Stat(left); !os.IsNotExist(err) {
			t.Fatalf("the revoked host left %s: %v", left, err)
		}
	}
}

func TestAgentStopsWhenItsJoinIsRefused(t *testing.T) {
	t.Parallel()
	e := newEnv(t)
	a := e.startAgent(func(cfg *Config) { cfg.JoinToken = "lc_join_unknown" })
	if err := a.exited(t); !errors.Is(err, ErrEnrollmentRefused) {
		t.Fatalf("Run returned %v", err)
	}
}
