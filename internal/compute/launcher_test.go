package compute_test

import (
	"encoding/base64"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// launchFleet is a platform fleet on the emulator with a published agent.
func launchFleet(t *testing.T, f compute.Fleet) (owners, *awsEmulator, compute.AgentRelease) {
	t.Helper()
	emulator := newAWS(t)
	o := newOwners(t, fleetConfig(emulator.fleet(f)))
	return o, emulator, publish(t, o.compute)
}

// launched answers RunInstances with a new instance in the subnet's zone.
func launched(t *testing.T) awsHandler {
	t.Helper()
	zones := map[string]string{}
	for _, n := range fleetNetworks() {
		for _, s := range n.Subnets {
			zones[s.ID] = s.Zone
		}
	}
	var n atomic.Int64
	return func(call awsCall) awsReply {
		id := fmt.Sprintf("i-%017x", 0xa0000+n.Add(1))
		return runInstancesReply(id, call.Form.Get("InstanceType"), zones[call.Form.Get("SubnetId")])
	}
}

func launch(t *testing.T, o owners) int {
	t.Helper()
	n, err := o.compute.Launch(t.Context(), discard())
	if err != nil {
		t.Fatalf("launch: %v", err)
	}
	return n
}

// requestedHost buys a host for one pending container of spec.
func requestedHost(t *testing.T, o owners, workspace uuid.UUID, spec string) compute.HostID {
	t.Helper()
	pendingContainer(t, o.pool, workspace, newRelease(t, o.pool, workspace, spec), 1000, gib)
	if result := planCapacity(t, o); result.Requested != 1 {
		t.Fatalf("plan capacity %+v, want one host requested", result)
	}
	return compute.HostID(scan[uuid.UUID](t, o.pool, "select id from hosts where phase = 'requested' order by created_at desc limit 1"))
}

func TestLaunchRunsATaggedIdempotentInstanceThatEnrollsAsItsHost(t *testing.T) {
	o, emulator, release := launchFleet(t, compute.Fleet{})
	emulator.on("RunInstances", launched(t))
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	spot := requestedHost(t, o, dev, `{}`)
	onDemand := requestedHost(t, o, dev, `{"placement": {"preemptible": false}}`)

	if n := launch(t, o); n != 2 {
		t.Fatalf("launched %d, want 2", n)
	}
	calls := map[string]awsCall{}
	for _, c := range emulator.calls("RunInstances") {
		calls[c.Form.Get("ClientToken")] = c
	}
	for _, host := range []compute.HostID{spot, onDemand} {
		call, ok := calls[host.String()]
		if !ok {
			t.Fatalf("no RunInstances with client token %s among %d calls", host, len(calls))
		}
		form := call.Form
		tags := instanceTags(form)
		if tags["lazycloud:fleet"] != "lazycloud-test" || tags["lazycloud:host-id"] != host.String() {
			t.Errorf("instance tags %v, want the fleet and host id", tags)
		}
		if s := form.Get("SubnetId"); s != "subnet-east-a" && s != "subnet-east-b" {
			t.Errorf("subnet %q, want a us-east-2 fleet subnet", s)
		}
		if form.Get("SecurityGroupId.1") != "sg-east" || form.Get("IamInstanceProfile.Name") != "lazycloud-node" ||
			form.Get("ClientToken") != host.String() || form.Get("MetadataOptions.HttpTokens") != "required" {
			t.Errorf("launch parameters %v", form)
		}
		data, err := base64.StdEncoding.DecodeString(form.Get("UserData"))
		if err != nil {
			t.Fatal(err)
		}
		for _, arg := range []string{
			"--cloud-host-id '" + host.String() + "'", "--agent-version '1.0.0'",
			"--agent-sha256 '" + release.SHA256["amd64"] + "'", "--server 'hosts.lazycloud.test:443'", "--server-tls",
		} {
			if !strings.Contains(string(data), arg) {
				t.Errorf("user data lacks %s:\n%s", arg, data)
			}
		}
		if call.AccessKey != platformAccessKey {
			t.Errorf("platform launch signed by %q", call.AccessKey)
		}
	}
	if m := calls[spot.String()].Form.Get("InstanceMarketOptions.MarketType"); m != "spot" {
		t.Errorf("preemptible host market %q, want spot", m)
	}
	if m := calls[onDemand.String()].Form.Get("InstanceMarketOptions.MarketType"); m != "" {
		t.Errorf("on-demand host asked for market %q", m)
	}
	var phase, instance, zone string
	if err := o.pool.QueryRow(t.Context(), "select phase, instance_id, availability_zone from hosts where id = $1",
		uuid.UUID(spot)).Scan(&phase, &instance, &zone); err != nil {
		t.Fatal(err)
	}
	if phase != string(compute.PhaseProvisioning) || !strings.HasPrefix(instance, "i-") || !strings.HasPrefix(zone, "us-east-2") {
		t.Fatalf("launched host %s %q in %q, want provisioning with its instance and zone", phase, instance, zone)
	}
}

func TestCapacityRefusalCoolsTheOfferAndTheNextPassBuysAnother(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	emulator.on("RunInstances", func(call awsCall) awsReply {
		return ec2Error(http.StatusInternalServerError, "InsufficientInstanceCapacity",
			"We currently do not have sufficient "+call.Form.Get("InstanceType")+" capacity in the Availability Zone you requested.")
	})
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	refused := requestedHost(t, o, dev, `{}`)
	offer := scan[string](t, o.pool, "select region || '/' || instance_type || '/' || market from hosts where id = $1", uuid.UUID(refused))

	if n := launch(t, o); n != 0 {
		t.Fatalf("launched %d with no capacity", n)
	}
	if phase, _ := hostPhase(t, o.pool, refused); phase != string(compute.PhaseFailed) {
		t.Fatalf("refused host is %s, want failed", phase)
	}
	cooled := scan[string](t, o.pool, "select region || '/' || instance_type || '/' || market from capacity_cooldowns where until > now()")
	if cooled != offer {
		t.Fatalf("cooldown on %s, want the refused offer %s", cooled, offer)
	}
	if result := planCapacity(t, o); result.Requested != 1 {
		t.Fatalf("next pass %+v, want another host for the container", result)
	}
	next := scan[string](t, o.pool, "select region || '/' || instance_type || '/' || market from hosts where phase = 'requested'")
	if next == offer {
		t.Fatalf("next pass bought the cooling offer %s again", next)
	}
}

func TestLaunchErrorsRetryWithTheSameClientTokenUntilBounded(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	var failing atomic.Bool
	failing.Store(true)
	succeed := launched(t)
	emulator.on("RunInstances", func(call awsCall) awsReply {
		if failing.Load() {
			return ec2Error(http.StatusInternalServerError, "InternalError", "An internal error has occurred")
		}
		return succeed(call)
	})
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	host := requestedHost(t, o, dev, `{}`)
	expireLeases := func() {
		run(t, o.pool, "update hosts set launch_lease_until = now() - interval '1 second' where launch_lease_until is not null")
	}

	launch(t, o)
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseRequested) {
		t.Fatalf("host after a transient error is %s, want requested for a retry", phase)
	}
	launch(t, o)
	if n := len(emulator.calls("RunInstances")); n != 1 {
		t.Fatalf("%d launches while the first launcher's lease holds, want 1", n)
	}
	expireLeases()
	failing.Store(false)
	if n := launch(t, o); n != 1 {
		t.Fatalf("launched %d after the lease passed, want 1", n)
	}
	for _, c := range emulator.calls("RunInstances") {
		if c.Form.Get("ClientToken") != host.String() {
			t.Fatalf("retry used client token %q, want the host id so AWS returns the same instance", c.Form.Get("ClientToken"))
		}
	}
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseProvisioning) {
		t.Fatalf("host is %s, want provisioning", phase)
	}

	// A host whose launches keep failing without an answer fails on its
	// fifth attempt and cools nothing down.
	failing.Store(true)
	stuck := requestedHost(t, o, dev, `{"placement": {"preemptible": false}}`)
	for attempt := 1; attempt <= 5; attempt++ {
		expireLeases()
		launch(t, o)
		phase, _ := hostPhase(t, o.pool, stuck)
		if want := map[bool]compute.Phase{true: compute.PhaseFailed, false: compute.PhaseRequested}[attempt == 5]; phase != string(want) {
			t.Fatalf("after attempt %d the host is %s, want %s", attempt, phase, want)
		}
	}
	if n := scan[int](t, o.pool, "select count(*)::int from capacity_cooldowns"); n != 0 {
		t.Fatalf("%d cooldowns after errors that were not capacity refusals", n)
	}
}
