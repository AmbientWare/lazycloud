package agent

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

func TestSleptSinceCountsOnlyTheAttemptsBoot(t *testing.T) {
	t.Parallel()
	attempt := sleepAttempt{ID: "a", BootID: "boot", Gap: 5 * time.Second}
	for _, c := range []struct {
		name    string
		attempt sleepAttempt
		boot    string
		gap     time.Duration
		want    time.Duration
	}{
		{"slept in the attempt's boot", attempt, "boot", 35 * time.Second, 30 * time.Second},
		{"clock noise", attempt, "boot", 5*time.Second + 300*time.Millisecond, 0},
		{"cold boot", attempt, "other", 35 * time.Second, 0},
		{"no attempt", sleepAttempt{}, "boot", 35 * time.Second, 0},
	} {
		if got := sleptSince(c.attempt, c.boot, c.gap); got != c.want {
			t.Errorf("%s: slept %s, want %s", c.name, got, c.want)
		}
	}
}

func TestKernelClockWaitsForAJumpUntilCancelled(t *testing.T) {
	t.Parallel()
	clock, err := openKernelClock()
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = clock.close() }()
	if clock.gap() < 0 {
		t.Fatalf("negative sleep gap %s", clock.gap())
	}
	ctx, cancel := context.WithTimeout(t.Context(), 200*time.Millisecond)
	defer cancel()
	if err := clock.jumped(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("jumped without a clock change: %v", err)
	}
}

// After a resume a Spot notice whose reclaim time passed in the sleep no
// longer stands, a later one does, and the watcher reads IMDS at once.
func TestResumeRearmsTheSpotNoticeFromTheWallClock(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		reclaim time.Duration
		stands  bool
	}{{-time.Minute, false}, {time.Minute, true}} {
		a := &Agent{
			conns: newConnTracker(), reconnectNow: make(chan struct{}, 1), interruptionNow: make(chan struct{}, 1),
			interruption: &interruption{reason: "aws-ec2-spot-hibernate", reclaimAt: time.Now().Add(c.reclaim)},
		}
		a.resumed()
		if (a.interruption != nil) != c.stands {
			t.Errorf("reclaim in %s: notice stands %v", c.reclaim, a.interruption != nil)
		}
		select {
		case <-a.interruptionNow:
		default:
			t.Error("the Spot notice watcher was not woken")
		}
	}
}

func TestReserveRefusalDuringAnUpdateIsMarked(t *testing.T) {
	t.Parallel()
	for _, a := range []*Agent{{updating: true}, {trial: "v2"}} {
		ready := a.readyForReserve(t.Context(), &hostproto.PrepareReserve{AttemptId: "a"})
		if !ready.GetUpdating() || ready.GetRefused() == "" {
			t.Fatalf("an updating agent answered %v", ready)
		}
	}
}
