package agent

import (
	"context"
	"errors"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

// fakeClock stands in for the kernel's clocks: sleep grows the gap and
// delivers the clock jump a resume causes.
type fakeClock struct {
	gapNanos atomic.Int64
	jumps    chan struct{}
}

func newFakeClock() *fakeClock { return &fakeClock{jumps: make(chan struct{})} }

func (c *fakeClock) gap() time.Duration { return time.Duration(c.gapNanos.Load()) }

func (c *fakeClock) jumped(ctx context.Context) error {
	select {
	case <-c.jumps:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (*fakeClock) close() error { return nil }

func (c *fakeClock) sleep(t *testing.T, d time.Duration) {
	t.Helper()
	c.gapNanos.Add(int64(d))
	select {
	case c.jumps <- struct{}{}:
	case <-time.After(10 * time.Second):
		t.Fatal("the agent is not watching for clock jumps")
	}
}

func prepareCommand() *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_PrepareReserve{PrepareReserve: &hostproto.PrepareReserve{
		RequestId: uuid.NewString(), AttemptId: uuid.NewString(), Mode: hostproto.ReserveMode_RESERVE_MODE_HIBERNATE,
	}}}
}

func (s *serverSession) reserveReady(t *testing.T, attempt string) *hostproto.ReserveReady {
	t.Helper()
	return s.until(t, 30*time.Second, func(m *hostproto.HostMessage) bool {
		return m.GetReserveReady().GetAttemptId() == attempt
	}).GetReserveReady()
}

func TestSleptSinceCountsOnlyTheAttemptsBoot(t *testing.T) {
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

// A resume reconnects the session at once, without waiting for TCP to see
// the dead connection, and the new Hello names the attempt and how long the
// host slept.
func TestAgentReconnectsAndReportsTheSleepAfterAResume(t *testing.T) {
	e := newEnv(t)
	clock := newFakeClock()
	e.startAgent(func(c *Config) { c.clock = clock })
	s := e.session()

	prepare := prepareCommand()
	s.send(t, prepare)
	ready := s.reserveReady(t, prepare.GetPrepareReserve().GetAttemptId())
	if ready.GetRefused() != "" || ready.GetRequestId() != prepare.GetPrepareReserve().GetRequestId() ||
		ready.GetBootId() != s.hello.GetBootId() || ready.GetAgentVersion() != "test" {
		t.Fatalf("reserve ready %v", ready)
	}

	resumed := time.Now()
	clock.sleep(t, 42*time.Second)
	s = e.session()
	t.Logf("resume to new session: %s", time.Since(resumed))
	if time.Since(resumed) > 2*time.Second {
		t.Fatalf("the session took %s to reconnect after a resume", time.Since(resumed))
	}
	if got := s.hello.GetSleepAttemptId(); got != prepare.GetPrepareReserve().GetAttemptId() {
		t.Fatalf("hello names attempt %q", got)
	}
	if got := s.hello.GetSleptSeconds(); got < 41.9 || got > 42.1 {
		t.Fatalf("hello reports %v slept seconds", got)
	}
}

// The attempt survives a restart, as across a cold boot, so the next Hello
// still names the stop it came back from.
func TestAgentRemembersTheSleepAttemptAcrossRestarts(t *testing.T) {
	e := newEnv(t)
	first := e.startAgent(func(c *Config) { c.clock = newFakeClock() })
	s := e.session()
	prepare := prepareCommand()
	s.send(t, prepare)
	if ready := s.reserveReady(t, prepare.GetPrepareReserve().GetAttemptId()); ready.GetRefused() != "" {
		t.Fatalf("refused: %s", ready.GetRefused())
	}
	first.stop()
	e.startAgent(func(c *Config) { c.clock = newFakeClock() })
	if got := e.session().hello.GetSleepAttemptId(); got != prepare.GetPrepareReserve().GetAttemptId() {
		t.Fatalf("hello after restart names attempt %q", got)
	}
}

func TestAgentRefusesTheReserveWhileWorkRemains(t *testing.T) {
	e := newEnv(t)
	e.startAgent(func(c *Config) { c.clock = newFakeClock() })
	s := e.session()
	id := e.startReady(s, 1)

	prepare := prepareCommand()
	s.send(t, prepare)
	if ready := s.reserveReady(t, prepare.GetPrepareReserve().GetAttemptId()); !strings.Contains(ready.GetRefused(), "containers still run here") {
		t.Fatalf("a host running a container answered %v", ready)
	}

	s.send(t, stopCommand(id, 5))
	s.phase(t, id, exited)
	e.eventually("the stopped container's removal lets the host stop", func() bool {
		prepare := prepareCommand()
		s.send(t, prepare)
		return s.reserveReady(t, prepare.GetPrepareReserve().GetAttemptId()).GetRefused() == ""
	})
}

func TestReserveBlockerRefusesDuringAnUpdate(t *testing.T) {
	for _, a := range []*Agent{{updating: true}, {trial: "v2"}} {
		if got := a.reserveBlocker(t.Context()); got != "an agent update is in flight" {
			t.Fatalf("an updating agent answered %q", got)
		}
	}
}
