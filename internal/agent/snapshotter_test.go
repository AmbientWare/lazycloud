package agent

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/imagefs/layersource"
)

// TestTheSnapshotterIsReplacedOnlyWhenIdle: an agent whose release brings
// another snapshotter keeps the running one while a container runs, and
// installs and restarts the release's once none does; a start right after
// mounts its volume through the new one.
func TestTheSnapshotterIsReplacedOnlyWhenIdle(t *testing.T) {
	installed, err := os.ReadFile(layersource.Binary)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := writeFileAtomic(layersource.Binary, installed, 0o755); err != nil { //nolint:gosec // An executable.
			t.Error(err)
		}
	})
	release := t.TempDir()
	newer := append(append([]byte{}, installed...), 0)
	if err := os.WriteFile(filepath.Join(release, layersource.Executable), newer, 0o755); err != nil { //nolint:gosec // An executable.
		t.Fatal(err)
	}
	executable := filepath.Join(release, AgentExecutable)
	if err := os.WriteFile(executable, nil, 0o755); err != nil { //nolint:gosec // An executable.
		t.Fatal(err)
	}
	inRelease := func(c *Config) { c.Executable = executable }

	e, s, store := volumeEnv(t)
	busy := e.startReady(s, 1)
	e.running.stop()
	e.startAgent(inRelease)
	s = e.session()
	s.adoptedReady(t, busy)
	if running, _ := os.ReadFile(layersource.Binary); !bytes.Equal(running, installed) {
		t.Fatal("the snapshotter was replaced while a container ran")
	}

	s.send(t, stopCommand(busy, 1))
	s.phase(t, busy, exited)
	e.running.stop()
	e.startAgent(inRelease)
	s = e.session()
	if running, _ := os.ReadFile(layersource.Binary); !bytes.Equal(running, newer) {
		t.Fatal("the idle host kept its snapshotter")
	}
	workspace, volume := uuid.NewString(), uuid.NewString()
	s.send(t, store.grant(workspace))
	id := e.startVolume(s, serveSource(t, "testdata/volumes"), workspace, volume)
	e.write(id, "/volumes/data/after-swap.txt", "swapped")
	if got := e.read(id, "/volumes/data/after-swap.txt"); got != `"swapped"` {
		t.Fatalf("read after the swap: %s", got)
	}
}
