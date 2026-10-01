package compute_test

import (
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// cloudHost is a ready platform instance in us-east-2.
func cloudHost(t *testing.T, o owners, market compute.Market, instance string) compute.HostID {
	t.Helper()
	return newHost(t, o.pool, hostSpec{
		Provider: compute.ProviderAWS, Market: market, Region: "us-east-2", Zone: "us-east-2a", InstanceID: instance,
	})
}

func idleSince(t *testing.T, o owners, host compute.HostID, ago time.Duration) {
	t.Helper()
	run(t, o.pool, "update hosts set idle_since = now() - make_interval(secs => $2) where id = $1", uuid.UUID(host), ago.Seconds())
}

// terminated lists the instance ids every TerminateInstances call named.
func terminated(e *awsEmulator) []string {
	var out []string
	for _, c := range e.calls("TerminateInstances") {
		out = append(out, list(c.Form, "InstanceId")...)
	}
	slices.Sort(out)
	return out
}

func TestIdleHostsBeyondTheHeadroomFloorDrainAndTerminate(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{HeadroomFloor: 1, IdleTimeout: 5 * time.Minute})
	emulator.on("TerminateInstances", terminateInstancesReply)
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	oldest := cloudHost(t, o, compute.MarketSpot, "i-0000000000000a001")
	idle := cloudHost(t, o, compute.MarketSpot, "i-0000000000000a002")
	recent := cloudHost(t, o, compute.MarketSpot, "i-0000000000000a003")
	busy := cloudHost(t, o, compute.MarketSpot, "i-0000000000000a004")
	other := cloudHost(t, o, compute.MarketOnDemand, "i-0000000000000a005")
	idleSince(t, o, oldest, 30*time.Minute)
	idleSince(t, o, idle, 20*time.Minute)
	idleSince(t, o, recent, time.Minute)
	idleSince(t, o, busy, time.Hour)
	idleSince(t, o, other, time.Hour)
	runningAttempt(t, o.pool, dev, newRelease(t, o.pool, dev, `{}`), busy)

	result, err := o.compute.Retire(t.Context(), discard())
	if err != nil {
		t.Fatal(err)
	}
	if result.Drained != 1 || result.Terminated != 1 {
		t.Fatalf("retire %+v, want one host drained and terminated", result)
	}
	if got := terminated(emulator); !slices.Equal(got, []string{"i-0000000000000a002"}) {
		t.Fatalf("terminated %v, want only the idle host past the floor and the idle window", got)
	}
	for host, want := range map[compute.HostID]compute.Phase{
		oldest: compute.PhaseReady, idle: compute.PhaseTerminating, recent: compute.PhaseReady,
		busy: compute.PhaseReady, other: compute.PhaseReady,
	} {
		if phase, _ := hostPhase(t, o.pool, host); phase != string(want) {
			t.Errorf("host %s is %s, want %s", host, phase, want)
		}
	}
	if result, err := o.compute.Retire(t.Context(), discard()); err != nil || result.Terminated != 0 {
		t.Fatalf("second pass %+v %v, want nothing more terminated", result, err)
	}
}

func TestReconcileFollowsWhatEC2Reports(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	gone := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Phase: compute.PhaseTerminating, Region: "us-east-2", InstanceID: "i-0000000000000b001"})
	vanished := cloudHost(t, o, compute.MarketSpot, "i-0000000000000b002")
	slow := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Phase: compute.PhaseProvisioning, Region: "us-east-2", InstanceID: "i-0000000000000b003"})
	run(t, o.pool, "update hosts set launched_at = now() - interval '20 minutes' where id = $1", uuid.UUID(slow))
	starting := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Phase: compute.PhaseProvisioning, Region: "us-east-2", InstanceID: "i-0000000000000b004"})
	stopped := cloudHost(t, o, compute.MarketOnDemand, "i-0000000000000b007")
	// A launch whose answer was lost: the host is still requested.
	unanswered := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Phase: compute.PhaseRequested, Region: "us-east-2"})
	w := runningAttempt(t, o.pool, dev, newRelease(t, o.pool, dev, `{}`), vanished)
	tags := func(host string) map[string]string {
		return map[string]string{"lazycloud:fleet": "lazycloud-test", "lazycloud:host-id": host, "Name": "lazycloud"}
	}
	emulator.on("DescribeInstances", func(awsCall) awsReply {
		return describeInstancesReply(
			ec2Instance{ID: "i-0000000000000b003", State: "running", Tags: tags(slow.String())},
			ec2Instance{ID: "i-0000000000000b004", State: "running", Tags: tags(starting.String())},
			ec2Instance{ID: "i-0000000000000b005", State: "running", Tags: tags(uuid.NewString())},
			ec2Instance{ID: "i-0000000000000b006", State: "pending", Tags: tags(unanswered.String())},
			ec2Instance{ID: "i-0000000000000b007", State: "stopped", Tags: tags(stopped.String())},
			ec2Instance{ID: "i-0000000000000b008", State: "terminated", Tags: tags(uuid.NewString())},
		)
	})
	emulator.on("TerminateInstances", terminateInstancesReply)

	if err := o.compute.Reconcile(t.Context(), discard()); err != nil {
		t.Fatal(err)
	}
	describe := emulator.calls("DescribeInstances")
	if len(describe) != 1 || describe[0].Form.Get("Filter.1.Name") != "tag:lazycloud:fleet" ||
		describe[0].Form.Get("Filter.1.Value.1") != "lazycloud-test" {
		t.Fatalf("DescribeInstances calls %v, want one filtered by the fleet tag", describe)
	}
	if got, want := terminated(emulator), []string{"i-0000000000000b003", "i-0000000000000b005", "i-0000000000000b007"}; !slices.Equal(got, want) {
		t.Fatalf("terminated %v, want the timed-out, orphaned and stopped instances %v", got, want)
	}
	for host, want := range map[compute.HostID]struct {
		phase   compute.Phase
		failure compute.Failure
	}{
		gone:       {compute.PhaseDeleted, ""},
		vanished:   {compute.PhaseFailed, compute.FailureProviderGone},
		slow:       {compute.PhaseFailed, compute.FailureBootstrapTimedOut},
		starting:   {compute.PhaseBooting, ""},
		stopped:    {compute.PhaseFailed, compute.FailureProviderStopped},
		unanswered: {compute.PhaseRequested, ""},
	} {
		phase, failure := hostPhase(t, o.pool, host)
		if phase != string(want.phase) || (failure == nil) != (want.failure == "") || (failure != nil && *failure != string(want.failure)) {
			t.Errorf("host %s is %s (%v), want %s (%s)", host, phase, failure, want.phase, want.failure)
		}
	}
	assertRetried(t, o.pool, w)
}
