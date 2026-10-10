package agent

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

// TestAgentStopsWhenItsCredentialIsRevoked: a removed host's agent stops
// and leaves no identity, slice or storage key behind.
func TestAgentStopsWhenItsCredentialIsRevoked(t *testing.T) {
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
	e := newEnv(t)
	a := e.startAgent(func(cfg *Config) { cfg.JoinToken = "lc_join_unknown" })
	if err := a.exited(t); !errors.Is(err, ErrEnrollmentRefused) {
		t.Fatalf("Run returned %v", err)
	}
}
