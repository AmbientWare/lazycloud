//go:build hostruntime

package agent

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

func TestAgentUpdatesItselfAndCommitsTheRelease(t *testing.T) {
	t.Parallel()
	e := newEnv(t)
	root := installRoot(t)
	if err := os.WriteFile(filepath.Join(e.stateDir, RejectedFile), []byte("0.9.0\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	running := func(version string) func(*Config) {
		return func(cfg *Config) {
			cfg.AgentRoot, cfg.Version = root, version
			cfg.Executable = filepath.Join(root, "releases", version, AgentExecutable)
		}
	}
	first := e.startAgent(running("1.0.0"))
	s := e.session()
	if !s.hello.GetUpdatable() || s.hello.GetRejectedVersion() != "0.9.0" || s.hello.GetAgentVersion() != "1.0.0" {
		t.Fatalf("hello %v", s.hello)
	}
	id := e.startReady(s, 1)

	// The rejected release is not installed again.
	s.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Update{Update: serveRelease(t, "0.9.0", agentRelease(t, "0.9.0"))}})
	began := time.Now()
	s.send(t, &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Update{Update: serveRelease(t, "2.0.0", agentRelease(t, "2.0.0"))}})
	if err := first.exited(t); !errors.Is(err, ErrUpdateInstalled) {
		t.Fatalf("Run returned %v", err)
	}
	t.Logf("update offer to agent exit: %s", time.Since(began))
	if linkTarget(t, root, "current") != "releases/2.0.0" || linkTarget(t, root, "previous") != "releases/1.0.0" {
		t.Fatalf("current %q, previous %q", linkTarget(t, root, "current"), linkTarget(t, root, "previous"))
	}
	if got := readMarker(e.stateDir, TrialFile); got != "2.0.0" {
		t.Fatalf("trial marker %q", got)
	}
	if _, err := os.Stat(filepath.Join(root, "releases", "0.9.0")); !os.IsNotExist(err) {
		t.Fatal("the rejected release was installed")
	}

	// A stale release no container uses is pruned once the new one commits.
	if err := os.MkdirAll(filepath.Join(root, "releases", "0.5.0"), 0o755); err != nil {
		t.Fatal(err)
	}
	e.startAgent(running("2.0.0"))
	s = e.session()
	if containers := s.hello.GetContainers(); len(containers) != 1 || containers[0].GetContainerId() != id || containers[0].GetPhase() == exited {
		t.Fatalf("the container did not survive the update: %v", containers)
	}
	e.eventually("the release commits", func() bool { return readMarker(e.stateDir, TrialFile) == "" })
	e.eventually("the stale release is pruned", func() bool {
		_, err := os.Stat(filepath.Join(root, "releases", "0.5.0"))
		return os.IsNotExist(err)
	})
	for _, version := range []string{"1.0.0", "2.0.0"} {
		if _, err := os.Stat(filepath.Join(root, "releases", version)); err != nil {
			t.Fatalf("release %s was pruned: %v", version, err)
		}
	}
}
