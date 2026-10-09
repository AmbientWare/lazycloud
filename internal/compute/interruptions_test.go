package compute_test

import (
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// An interrupted host drains at once, its work stops just before the
// reclaim, and the provider rather than the fleet ends it: EC2 charges
// nothing for a Spot instance it reclaims in its first hour. The fleet
// terminates it only once the reclaim is overdue.
func TestInterruptedHostDrainsAtOnceAndIsPreemptedBeforeReclaim(t *testing.T) {
	ctx := t.Context()
	o := newOwners(t, compute.Config{})
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	release := newRelease(t, o.pool, dev, `{}`)
	later := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Market: compute.MarketSpot, Region: "us-east-2", CPU: 16000, Memory: 32 * gib})
	soon := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Market: compute.MarketSpot, Region: "us-east-2", CPU: 16000, Memory: 32 * gib,
		InstanceID: "i-soon"})
	running := runningAttempt(t, o.pool, dev, release, later)
	starting := scan[uuid.UUID](t, o.pool, `insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
values ($1, $2, 'starting', $3, 1, 1000, 1 << 28, now()) returning id`, dev, release, uuid.UUID(later))
	doomed := runningAttempt(t, o.pool, dev, release, soon)

	reclaim := time.Now().Add(2 * time.Minute).Truncate(time.Microsecond)
	if err := o.compute.ReportInterruption(ctx, later, "spot", reclaim); err != nil {
		t.Fatal(err)
	}
	if err := o.compute.ReportInterruption(ctx, soon, "spot", time.Now().Add(10*time.Second)); err != nil {
		t.Fatal(err)
	}
	var phase, capacity string
	var at time.Time
	if err := o.pool.QueryRow(ctx, "select phase, capacity_state, interruption_at from hosts where id = $1",
		uuid.UUID(later)).Scan(&phase, &capacity, &at); err != nil {
		t.Fatal(err)
	}
	if phase != string(compute.PhaseDraining) || capacity != string(compute.CapacityPreempting) || !at.Equal(reclaim) {
		t.Fatalf("interrupted host %s/%s reclaimed at %v, want draining/preempting at %v", phase, capacity, at, reclaim)
	}
	for _, c := range []uuid.UUID{running.container, starting} {
		if state := scan[string](t, o.pool, "select state from containers where id = $1", c); state != "draining" {
			t.Errorf("container %s on the interrupted host is %s, want draining", c, state)
		}
	}
	// A repeated notice changes nothing.
	if err := o.compute.ReportInterruption(ctx, later, "spot", reclaim.Add(time.Minute)); err != nil {
		t.Fatal(err)
	}
	if again := scan[time.Time](t, o.pool, "select interruption_at from hosts where id = $1", uuid.UUID(later)); !again.Equal(reclaim) {
		t.Fatalf("a repeated notice moved the reclaim time to %v", again)
	}
	pending := pendingContainer(t, o.pool, dev, release, 1000, gib)
	if n := place(t, o); n != 0 || containerHost(t, o.pool, pending) != nil {
		t.Fatalf("placed %d containers on interrupted hosts", n)
	}

	preempted, err := o.compute.Preempt(ctx, discard())
	if err != nil {
		t.Fatal(err)
	}
	if preempted != 1 {
		t.Fatalf("preempted %d hosts, want only the one reclaimed within the lead", preempted)
	}
	assertRetried(t, o.pool, doomed)
	if _, err := o.compute.Retire(ctx, discard()); err != nil {
		t.Fatal(err)
	}
	if phase, _ := hostPhase(t, o.pool, soon); phase != string(compute.PhaseDraining) {
		t.Fatalf("preempted host is %s, want draining until the provider reclaims it", phase)
	}
	if phase, _ := hostPhase(t, o.pool, later); phase != string(compute.PhaseDraining) {
		t.Fatalf("host reclaimed later is %s, want still draining", phase)
	}
	if state := scan[string](t, o.pool, "select state from attempts where id = $1", running.attempt); state != "running" {
		t.Fatalf("attempt on the host reclaimed later is %s, want still running", state)
	}
	if n, err := o.compute.Preempt(ctx, discard()); err != nil || n != 0 {
		t.Fatalf("second preemption pass: %d %v, want nothing", n, err)
	}

	run(t, o.pool, "update hosts set interruption_at = now() - make_interval(secs => $2) where id = $1",
		uuid.UUID(soon), (compute.ReclaimGrace + time.Second).Seconds())
	if _, err := o.compute.Retire(ctx, discard()); err != nil {
		t.Fatal(err)
	}
	if phase, _ := hostPhase(t, o.pool, soon); phase != string(compute.PhaseTerminating) {
		t.Fatalf("host the provider did not reclaim in time is %s, want terminating", phase)
	}
}
