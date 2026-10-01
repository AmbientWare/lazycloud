package compute_test

import (
	"errors"
	"slices"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// The platform generates every external ID, and a role backs one
// connection, so one account cannot aim the platform at another's role.
func TestConnectionsCannotChooseExternalIDsOrShareRoles(t *testing.T) {
	ctx := t.Context()
	o, _, _ := connectionFleet(t)
	alice, bob := newUser(t, o.pool, "alice@example.com"), newUser(t, o.pool, "bob@example.com")
	role := "arn:aws:iam::222222222222:role/shared"
	var invalid *compute.InvalidError
	if _, err := o.compute.Connect(ctx, alice, compute.ConnectRequest{
		AWSAccountID: "222222222222", RoleARN: role, Networks: fleetNetworks(), ExternalID: strings.Repeat("x", 40),
	}); !errors.As(err, &invalid) {
		t.Fatalf("connect with a chosen external ID: %v, want InvalidError", err)
	}
	if _, err := o.compute.Connect(ctx, alice, compute.ConnectRequest{AWSAccountID: "222222222222", RoleARN: role, Networks: fleetNetworks()}); err != nil {
		t.Fatal(err)
	}
	var conflict *compute.ConflictError
	if _, err := o.compute.Connect(ctx, bob, compute.ConnectRequest{AWSAccountID: "222222222222", RoleARN: role, Networks: fleetNetworks()}); !errors.As(err, &conflict) {
		t.Fatalf("connect another account to the same role: %v, want ConflictError", err)
	}
}

// A cloud host that enrolled but never opened a session, or whose
// preflight failed, is failed and its instance terminated rather than
// counted as capacity on the way forever.
func TestCloudHostsThatNeverServeAreReplaced(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	stuck := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Phase: compute.PhaseJoining, Region: "us-east-2", InstanceID: "i-0000000000000d001"})
	run(t, o.pool, "update hosts set phase_at = now() - interval '1 hour', launched_at = now() - interval '1 hour' where id = $1", uuid.UUID(stuck))
	emulator.on("DescribeInstances", func(call awsCall) awsReply {
		if !strings.Contains(call.Header.Get("Authorization"), "/us-east-2/") {
			return describeInstancesReply()
		}
		return describeInstancesReply(ec2Instance{ID: "i-0000000000000d001", State: "running",
			Tags: map[string]string{"lazycloud:fleet": "lazycloud-test", "lazycloud:host-id": uuid.UUID(stuck).String()}})
	})
	emulator.on("TerminateInstances", terminateInstancesReply)
	if err := o.compute.Reconcile(t.Context(), discard()); err != nil {
		t.Fatal(err)
	}
	if phase, failure := hostPhase(t, o.pool, stuck); phase != string(compute.PhaseFailed) || failure == nil || *failure != string(compute.FailureEnrollment) {
		t.Fatalf("joining host past the boot timeout is %s (%v), want failed agent_enrollment_failed", phase, failure)
	}
	if got := terminated(emulator); !slices.Equal(got, []string{"i-0000000000000d001"}) {
		t.Fatalf("terminated %v, want the stuck instance", got)
	}
}

// A terminating host stays terminating until EC2 reports the instance
// terminated, so a termination that did not take is retried; a new
// instance EC2 does not list yet is not taken for gone; and a configured
// region without hosts is still swept for orphans.
func TestReconcileWaitsForEC2AndSweepsEveryRegion(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	terminating := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Phase: compute.PhaseTerminating, Region: "us-east-2", InstanceID: "i-0000000000000e001"})
	fresh := cloudHost(t, o, compute.MarketSpot, "i-0000000000000e002")
	run(t, o.pool, "update hosts set launched_at = now() where id = $1", uuid.UUID(fresh))
	emulator.on("DescribeInstances", func(call awsCall) awsReply {
		if strings.Contains(call.Header.Get("Authorization"), "/us-west-1/") {
			return describeInstancesReply(ec2Instance{ID: "i-0000000000000e003", State: "running",
				Tags: map[string]string{"lazycloud:fleet": "lazycloud-test", "lazycloud:host-id": uuid.NewString()}})
		}
		return describeInstancesReply(ec2Instance{ID: "i-0000000000000e001", State: "running",
			Tags: map[string]string{"lazycloud:fleet": "lazycloud-test", "lazycloud:host-id": uuid.UUID(terminating).String()}})
	})
	emulator.on("TerminateInstances", terminateInstancesReply)
	if err := o.compute.Reconcile(t.Context(), discard()); err != nil {
		t.Fatal(err)
	}
	if phase, _ := hostPhase(t, o.pool, terminating); phase != string(compute.PhaseTerminating) {
		t.Fatalf("host whose instance still runs is %s, want terminating", phase)
	}
	if phase, _ := hostPhase(t, o.pool, fresh); phase != string(compute.PhaseReady) {
		t.Fatalf("host launched just now that EC2 does not list yet is %s, want ready", phase)
	}
	got := terminated(emulator)
	slices.Sort(got)
	if !slices.Equal(got, []string{"i-0000000000000e001", "i-0000000000000e003"}) {
		t.Fatalf("terminated %v, want the instance still running and the orphan in us-west-1", got)
	}
}

// When the host bought for a container joins and still cannot take it,
// that offer cools down and the next pass buys another instead of the same
// one again.
func TestABoughtHostThatCannotTakeItsContainerCoolsItsOffer(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{}))
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	container := pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{}`), 1000, gib)
	if result := planCapacity(t, o); result.Requested != 1 {
		t.Fatalf("first pass %+v, want one host", result)
	}
	bought := scan[uuid.UUID](t, o.pool, "select capacity_host_id from containers where id = $1", container)
	first := scan[string](t, o.pool, "select region || '/' || instance_type || '/' || market from hosts where id = $1", bought)
	if result := planCapacity(t, o); result.Requested != 0 {
		t.Fatalf("pass while the host is on the way %+v, want nothing more bought", result)
	}
	// It joins smaller than predicted.
	run(t, o.pool, `update hosts set phase = 'ready', state = 'online', last_seen_at = now(), token_hash = sha256('t'),
	                cpu_millis = 500, memory_bytes = 1 << 30 where id = $1`, bought)
	if result := planCapacity(t, o); result.Requested != 1 {
		t.Fatalf("pass after the host joined too small %+v, want another host", result)
	}
	second := scan[string](t, o.pool, "select h.region || '/' || h.instance_type || '/' || h.market from containers c join hosts h on h.id = c.capacity_host_id where c.id = $1", container)
	if second == first {
		t.Fatalf("bought %s again, want another offer once %s cooled down", second, first)
	}
}

// Agent releases reach a share of hosts, and an updating host is not lost
// while it restarts.
func TestAgentReleasesRollOutInStagesAndUpdatingHostsStayUnlost(t *testing.T) {
	ctx := t.Context()
	o := newOwners(t, machineConfig())
	release := compute.AgentRelease{Version: "2.0.0", SHA256: map[string]string{"amd64": strings.Repeat("c", 64)}, RolloutPercent: 50}
	if err := o.compute.PublishAgentRelease(ctx, release); err != nil {
		t.Fatal(err)
	}
	reached := 0
	var updating compute.HostID
	for range 40 {
		host := newHost(t, o.pool, hostSpec{})
		update, err := o.compute.UpdateFor(ctx, host, "1.0.0", "", true)
		if err != nil {
			t.Fatal(err)
		}
		if update != nil {
			reached++
			updating = host
		}
	}
	if reached == 0 || reached == 40 {
		t.Fatalf("a 50%% rollout reached %d of 40 hosts", reached)
	}
	if err := o.compute.UpdateSent(ctx, updating); err != nil {
		t.Fatal(err)
	}
	run(t, o.pool, "update hosts set last_seen_at = now() - interval '1 minute' where id = $1", uuid.UUID(updating))
	stale, err := compute.StaleHosts(ctx, o.pool, nil, 100)
	if err != nil {
		t.Fatal(err)
	}
	for _, h := range stale {
		if h.ID == updating {
			t.Fatal("a host restarting into an update counted as stale")
		}
	}
	release.RolloutPercent = 0
	if err := o.compute.PublishAgentRelease(ctx, release); err != nil {
		t.Fatalf("narrow the rollout: %v", err)
	}
	if update, err := o.compute.UpdateFor(ctx, updating, "1.0.0", "", true); err != nil || update != nil {
		t.Fatalf("a 0%% rollout offered %+v %v", update, err)
	}
}

// A fleet that launches instances needs a public install origin and server
// address they can reach with TLS.
func TestFleetConfigurationNeedsReachableEndpoints(t *testing.T) {
	fleet := compute.Fleet{Networks: fleetNetworks()}
	for name, c := range map[string]compute.Config{
		"no install URL":       {ServerAddress: "hosts.lazycloud.test:443", Fleet: fleet},
		"plain HTTP install":   {InstallURL: "http://lazycloud.test", ServerAddress: "hosts.lazycloud.test:443", Fleet: fleet},
		"loopback install":     {InstallURL: "https://127.0.0.1", ServerAddress: "hosts.lazycloud.test:443", Fleet: fleet},
		"no server address":    {InstallURL: "https://lazycloud.test", Fleet: fleet},
		"loopback server":      {InstallURL: "https://lazycloud.test", ServerAddress: "127.0.0.1:8081", Fleet: fleet},
		"plaintext to servers": {InstallURL: "https://lazycloud.test", ServerAddress: "hosts.lazycloud.test:443", ServerPlaintext: true, Fleet: fleet},
	} {
		if err := c.CheckFleet(); err == nil {
			t.Errorf("%s: accepted", name)
		}
	}
	if err := (compute.Config{InstallURL: "https://lazycloud.test", ServerAddress: "hosts.lazycloud.test:443", Fleet: fleet}).CheckFleet(); err != nil {
		t.Fatalf("a reachable configuration: %v", err)
	}
	if err := (compute.Config{}).CheckFleet(); err != nil {
		t.Fatalf("a stack without a fleet: %v", err)
	}
}

// Dropping a workspace from a machine drains that workspace's containers on
// it, and placement stops sending it more.
func TestDroppingAWorkspaceDrainsItsWorkOnTheMachine(t *testing.T) {
	ctx := t.Context()
	o := newOwners(t, machineConfig())
	alice := newUser(t, o.pool, "alice@example.com")
	dev, lab := newWorkspace(t, o.pool, "dev", alice), newWorkspace(t, o.pool, "lab", alice)
	publish(t, o.compute)
	joinMachine(t, o, alice, dev, lab)
	pinned := pendingContainer(t, o.pool, lab, newRelease(t, o.pool, lab, `{}`), 1000, gib)
	run(t, o.pool, "update releases set spec = '{\"placement\": {\"machine\": \"gpu-1\"}}' where id = (select release_id from containers where id = $1)", pinned)
	if n := place(t, o); n != 1 {
		t.Fatalf("placed %d, want the lab container on the machine", n)
	}
	// Its workload is deleted, so lab can be dropped.
	run(t, o.pool, "update workloads set desired_state = 'deleted'")
	if _, err := o.compute.UpdateMachineWorkspaces(ctx, alice, "gpu-1", []uuid.UUID{dev}); err != nil {
		t.Fatal(err)
	}
	if state := scan[string](t, o.pool, "select state from containers where id = $1", pinned); state != "draining" {
		t.Fatalf("lab container on the machine is %s, want draining", state)
	}

}

// A connection host launches only when the authorization names the node
// role its identity proof will be checked against.
func TestConnectionLaunchesNeedTheNodeRole(t *testing.T) {
	o, emulator, customer := connectionFleet(t)
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connected(t, o, customer, alice)
	run(t, o.pool, "update cloud_authorizations set node_role_arn = null where connection_id = $1", conn.ID)
	publish(t, o.compute)
	host := scan[uuid.UUID](t, o.pool, `
insert into hosts (name, state, kind, provider, connection_id, phase, cpu_millis, memory_bytes, region, instance_type, market)
values ('h', 'offline', 'connection', 'aws', $1, 'requested', 1000, 1 << 30, 'us-east-2', 'm7i.large', 'spot') returning id`, conn.ID)
	if _, err := o.compute.Launch(t.Context(), discard()); err != nil {
		t.Fatal(err)
	}
	if phase, _ := hostPhase(t, o.pool, compute.HostID(host)); phase != string(compute.PhaseFailed) {
		t.Fatalf("host without a node role is %s, want failed", phase)
	}
	if calls := emulator.calls("RunInstances"); len(calls) != 0 {
		t.Fatalf("RunInstances called %d times, want none", len(calls))
	}
}
