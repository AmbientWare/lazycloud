package edge

import (
	"testing"

	"github.com/google/uuid"
)

// One pod cannot take every TCP slot of an edge.
func TestTCPConnectionsAreBoundedPerTarget(t *testing.T) {
	var targets tcpTargets
	busy, other := uuid.New(), uuid.New()
	for range maxTCPPerTarget {
		if !targets.take(busy) {
			t.Fatal("a target was refused below its bound")
		}
	}
	if targets.take(busy) {
		t.Fatal("a target took more than its bound")
	}
	if !targets.take(other) {
		t.Fatal("another target was refused")
	}
	targets.give(busy)
	if !targets.take(busy) {
		t.Fatal("a released slot was not reusable")
	}
}
