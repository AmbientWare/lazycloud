package agent

import (
	"testing"

	"github.com/google/uuid"
)

// TestADyingMountFailsItsStartOrItsContainer: whichever the agent sees
// first, a mount appearing or its container exiting, a mount that dies
// either fails the start that waits for it or is lost to its container.
func TestADyingMountFailsItsStartOrItsContainer(t *testing.T) {
	t.Parallel()
	v := newVolumes(&Agent{})
	exitedFirst := v.newMounter(mounterName(uuid.NewString(), 0), "")
	if v.markExited(exitedFirst) || v.markUp(exitedFirst) {
		t.Fatal("a mount whose container exited before it appeared came up")
	}
	upFirst := v.newMounter(mounterName(uuid.NewString(), 0), "")
	if !v.markUp(upFirst) || !v.markExited(upFirst) {
		t.Fatal("a mount that came up and exited was not lost")
	}
}
