package compute

import (
	"testing"

	"github.com/google/uuid"
)

// A launch's client token follows its arguments once its walk has moved it:
// a launch retried as it was reuses its token, so EC2 runs one instance for
// it; one moved to another pool, or launched from another image, gets a new
// token, so EC2 does not refuse it as an idempotent request with other
// arguments (prod failed a host that way on 2026-10-15).
func TestALaunchTokenFollowsTheLaunchArguments(t *testing.T) {
	t.Parallel()
	h := ClaimHostActionsRow{ID: uuid.New(), InstanceType: "g6.xlarge", AvailabilityZone: "us-east-2a", LaunchPools: 1}
	same := launchToken(h, "ami-1")
	if same != launchToken(h, "ami-1") || len(same) > 64 {
		t.Fatalf("token %q is not stable or too long", same)
	}
	moved := h
	moved.AvailabilityZone = "us-east-2b"
	resized := h
	resized.InstanceType = "g6.2xlarge"
	if launchToken(moved, "ami-1") == same || launchToken(resized, "ami-1") == same || launchToken(h, "ami-2") == same {
		t.Fatal("a launch with other arguments reuses the token")
	}
}
