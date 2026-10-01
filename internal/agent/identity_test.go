package agent

import (
	"errors"
	"os"
	"testing"
)

func TestAgentStopsWhenItsCredentialIsRevoked(t *testing.T) {
	e := newEnv(t)
	a := e.startAgent()
	e.session()

	// Removing the machine replaces its token; the agent's next session is
	// refused.
	e.server.revoke()
	e.grpc.Stop()
	e.grpc, _ = e.server.serve(t, e.address)
	if err := a.exited(t); !errors.Is(err, ErrCredentialRevoked) {
		t.Fatalf("Run returned %v", err)
	}
	if _, err := os.Stat(identityPath(e.stateDir)); !os.IsNotExist(err) {
		t.Fatalf("the revoked identity remains: %v", err)
	}
}

func TestAgentStopsWhenItsJoinIsRefused(t *testing.T) {
	e := newEnv(t)
	a := e.startAgent(func(cfg *Config) { cfg.JoinToken = "lc_join_unknown" })
	if err := a.exited(t); !errors.Is(err, ErrEnrollmentRefused) {
		t.Fatalf("Run returned %v", err)
	}
}
