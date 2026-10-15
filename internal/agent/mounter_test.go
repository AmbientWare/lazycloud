package agent

import (
	"sync"
	"testing"

	"github.com/google/uuid"
)

// TestADyingMountFailsItsStartOrItsContainer: whichever the agent sees
// first, a mount appearing or its container exiting, a mount that dies
// either fails the start that waits for it or is lost to its container.
func TestADyingMountFailsItsStartOrItsContainer(t *testing.T) {
	t.Parallel()
	v := newVolumes(&Agent{})
	exitedFirst := v.newMounter(mounterName(uuid.NewString(), 0), "", "")
	if v.markExited(exitedFirst) || v.markUp(exitedFirst) {
		t.Fatal("a mount whose container exited before it appeared came up")
	}
	upFirst := v.newMounter(mounterName(uuid.NewString(), 0), "", "")
	if !v.markUp(upFirst) || !v.markExited(upFirst) {
		t.Fatal("a mount that came up and exited was not lost")
	}
}

// TestAMountKeyIsMadeOnce: starts and grant refreshes that ask for a
// mount's key at once all get the key its files hold, so the mount and
// the snapshotter never see different keys.
func TestAMountKeyIsMadeOnce(t *testing.T) {
	t.Parallel()
	v := newVolumes(&Agent{cfg: Config{StateDir: t.TempDir()}})
	name := mounterName(uuid.NewString(), 0)
	keys := make([]string, 8)
	var wg sync.WaitGroup
	for n := range keys {
		wg.Go(func() {
			key, err := v.mountKey(name)
			if err != nil {
				t.Error(err)
			}
			keys[n] = key
		})
	}
	wg.Wait()
	stored, err := readKey(v.mountKeys(name))
	if err != nil {
		t.Fatal(err)
	}
	for _, key := range keys {
		if key != stored {
			t.Fatalf("a caller got key %s; the mount's files hold %s", key, stored)
		}
	}
}
