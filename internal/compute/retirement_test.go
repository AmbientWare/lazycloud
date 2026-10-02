package compute_test

import (
	"slices"
	"strings"
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

// lightFor makes a host launched an hour ago lightly used for ago.
func lightFor(t *testing.T, o owners, host compute.HostID, ago time.Duration) {
	t.Helper()
	run(t, o.pool, "update hosts set launched_at = now() - interval '1 hour', light_since = now() - make_interval(secs => $2) where id = $1",
		uuid.UUID(host), ago.Seconds())
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

// Idle hosts leave only once lightly used past the idle wait and while the
// market's free room beyond its warm target covers them; a one-time Spot
// host cannot stop, so it drains and terminates.
func TestIdleHostsLeaveOnlyBeyondTheWarmTargetAndTerminate(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{IdleTimeout: 5 * time.Minute})
	emulator.on("TerminateInstances", terminateInstancesReply)
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	oldest := cloudHost(t, o, compute.MarketSpot, "i-0000000000000a001")
	idle := cloudHost(t, o, compute.MarketSpot, "i-0000000000000a002")
	recent := cloudHost(t, o, compute.MarketSpot, "i-0000000000000a003")
	busy := cloudHost(t, o, compute.MarketSpot, "i-0000000000000a004")
	other := cloudHost(t, o, compute.MarketOnDemand, "i-0000000000000a005")
	lightFor(t, o, oldest, 30*time.Minute)
	lightFor(t, o, idle, 20*time.Minute)
	lightFor(t, o, recent, time.Minute)
	lightFor(t, o, busy, time.Hour)
	lightFor(t, o, other, time.Hour)
	// Busy past the light-use share, so it does not consolidate either,
	// with a container that arrived before the forecast's window.
	run(t, o.pool, `
insert into containers (id, workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
values (uuidv7(interval '-1 hour'), $1, $2, 'ready', $3, 1, 1500, 1 << 28, now(), now())`, dev, newRelease(t, o.pool, dev, `{}`), uuid.UUID(busy))

	if result := plan(t, o); !result.Published || result.Drained != 2 {
		t.Fatalf("plan %+v, want a reserve pass that drains two hosts", result)
	}
	result, err := o.compute.Retire(t.Context(), discard())
	if err != nil {
		t.Fatal(err)
	}
	if result.Terminated != 2 {
		t.Fatalf("retire %+v, want the two drained hosts terminated", result)
	}
	if got := terminated(emulator); !slices.Equal(got, []string{"i-0000000000000a001", "i-0000000000000a002"}) {
		t.Fatalf("terminated %v, want the hosts idle past the wait beyond the warm target", got)
	}
	for host, want := range map[compute.HostID]compute.Phase{
		oldest: compute.PhaseTerminating, idle: compute.PhaseTerminating, recent: compute.PhaseReady,
		busy: compute.PhaseReady, other: compute.PhaseReady,
	} {
		if phase, _ := hostPhase(t, o.pool, host); phase != string(want) {
			t.Errorf("host %s is %s, want %s", host, phase, want)
		}
	}
}

func TestReconcileFollowsWhatEC2Reports(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	gone := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Phase: compute.PhaseTerminating, Region: "us-east-2", InstanceID: "i-0000000000000b001"})
	vanished := cloudHost(t, o, compute.MarketSpot, "i-0000000000000b002")
	// Past the grace EC2 gets to list a new instance.
	run(t, o.pool, "update hosts set launched_at = now() - interval '10 minutes' where id = $1", uuid.UUID(vanished))
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
	emulator.on("DescribeInstances", func(call awsCall) awsReply {
		if !strings.Contains(call.Header.Get("Authorization"), "/us-east-2/") {
			return describeInstancesReply()
		}
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
	// Every configured region is read, each by the fleet tag.
	describe := emulator.calls("DescribeInstances")
	if len(describe) != len(fleetNetworks()) {
		t.Fatalf("DescribeInstances %d times, want once per configured region", len(describe))
	}
	for _, call := range describe {
		if call.Form.Get("Filter.1.Name") != "tag:lazycloud:fleet" || call.Form.Get("Filter.1.Value.1") != "lazycloud-test" {
			t.Fatalf("DescribeInstances call %v, want it filtered by the fleet tag", call.Form)
		}
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
