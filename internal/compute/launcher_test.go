package compute_test

import (
	"encoding/base64"
	"fmt"
	"net/http"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/aws/retry"
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
			"--agent-sha256 '" + release.SHA256["amd64"] + "'", "--server 'hosts.lazycloud.test:443'",
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

// A host every pool refuses fails after three pools, each cooled, and the
// next pass buys from none of them.
func TestCapacityRefusalCoolsTheOfferAndTheNextPassBuysAnother(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	emulator.on("RunInstances", func(call awsCall) awsReply {
		return ec2Error(http.StatusInternalServerError, "InsufficientInstanceCapacity",
			"We currently do not have sufficient "+call.Form.Get("InstanceType")+" capacity in the Availability Zone you requested.")
	})
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	refused := requestedHost(t, o, dev, `{}`)
	offer := scan[string](t, o.pool, "select region || '/' || availability_zone_id || '/' || instance_type || '/' || market from hosts where id = $1", uuid.UUID(refused))

	if n := launch(t, o); n != 0 {
		t.Fatalf("launched %d with no capacity", n)
	}
	if phase, _ := hostPhase(t, o.pool, refused); phase != string(compute.PhaseFailed) {
		t.Fatalf("refused host is %s, want failed", phase)
	}
	cooled := scan[[]string](t, o.pool, "select array_agg(region || '/' || availability_zone_id || '/' || instance_type || '/' || market) from capacity_cooldowns where until > now()")
	if tried := len(emulator.calls("RunInstances")); tried != 3 || len(cooled) != tried || !slices.Contains(cooled, offer) {
		t.Fatalf("%d pools tried, cooldowns on %v, want three including the bought offer %s", tried, cooled, offer)
	}
	if result := planCapacity(t, o); result.Requested != 1 {
		t.Fatalf("next pass %+v, want another host for the container", result)
	}
	next := scan[string](t, o.pool, "select region || '/' || availability_zone_id || '/' || instance_type || '/' || market from hosts where phase = 'requested'")
	if slices.Contains(cooled, next) {
		t.Fatalf("next pass bought the cooling offer %s again", next)
	}
}

// refuseFirstPool refuses each host's launch in the pool it was bought
// from, whose client token is the host id, and launches it elsewhere.
func refuseFirstPool(t *testing.T, code string) awsHandler {
	t.Helper()
	succeed := launched(t)
	return func(call awsCall) awsReply {
		if _, err := uuid.Parse(call.Form.Get("ClientToken")); err == nil {
			return ec2Error(http.StatusInternalServerError, code, "refused "+call.Form.Get("InstanceType"))
		}
		return succeed(call)
	}
}

// pool is a RunInstances call's type, subnet and market.
func pool(call awsCall) string {
	return call.Form.Get("InstanceType") + " " + call.Form.Get("SubnetId") + " " + call.Form.Get("InstanceMarketOptions.MarketType")
}

// A launch EC2 refuses for capacity, quota or price moves on in the same
// pass to the next ranked pool that holds what the host was bought for,
// and only the pool it tried cools.
func TestARefusedPoolLaunchesTheNextPoolInTheSamePass(t *testing.T) {
	for _, code := range []string{"InsufficientInstanceCapacity", "MaxSpotInstanceCountExceeded", "SpotMaxPriceTooLow"} {
		o, emulator, _ := launchFleet(t, compute.Fleet{})
		emulator.on("RunInstances", refuseFirstPool(t, code))
		alice := newUser(t, o.pool, "alice@example.com")
		host := requestedHost(t, o, newWorkspace(t, o.pool, "dev", alice), `{}`)
		bought := scan[string](t, o.pool, "select region || '/' || instance_type || '/' || market from hosts where id = $1", uuid.UUID(host))
		usable := scan[int64](t, o.pool, "select memory_bytes from hosts where id = $1", uuid.UUID(host))

		if n := launch(t, o); n != 1 {
			t.Fatalf("%s: launched %d, want the host in its next pool", code, n)
		}
		calls := emulator.calls("RunInstances")
		var pools []string
		for _, c := range calls {
			pools = append(pools, pool(c))
		}
		if len(calls) != 2 || calls[1].Form.Get("ClientToken") != host.String()+"-1" || pools[0] == pools[1] ||
			calls[1].Form.Get("InstanceMarketOptions.MarketType") != "spot" {
			t.Fatalf("%s: launches %v, want the bought pool and then another Spot pool under a new token", code, pools)
		}
		var phase, instanceType string
		var memory int64
		if err := o.pool.QueryRow(t.Context(), "select phase, instance_type, memory_bytes from hosts where id = $1", uuid.UUID(host)).
			Scan(&phase, &instanceType, &memory); err != nil {
			t.Fatal(err)
		}
		if phase != string(compute.PhaseProvisioning) || instanceType != calls[1].Form.Get("InstanceType") || memory < usable {
			t.Fatalf("%s: host %s as %s with %d bytes, want provisioning in the pool launched, holding at least %d", code, phase, instanceType, memory, usable)
		}
		cooled := scan[[]string](t, o.pool, "select array_agg(region || '/' || instance_type || '/' || market) from capacity_cooldowns")
		if len(cooled) != 1 || cooled[0] != bought {
			t.Fatalf("%s: cooldowns %v, want only the refused pool %s", code, cooled, bought)
		}
	}
}

// A hibernating Spot reserve keeps its persistent request and hibernation
// in the pool it moves to.
func TestARefusedSpotReserveMovesOnHibernatingOnAPersistentRequest(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	emulator.on("RunInstances", refuseFirstPool(t, "InsufficientInstanceCapacity"))
	spotPrices(t, o)
	small, _ := compute.CatalogTypeNamed("m7i.large")
	usable := small.Usable(0)
	host := scan[uuid.UUID](t, o.pool, `
insert into hosts (name, state, kind, provider, phase, cpu_millis, memory_bytes, region, availability_zone, instance_type, market, reserve_mode)
values ('r', 'offline', 'platform', 'aws', 'requested', $1, $2, 'us-east-2', 'us-east-2a', 'm7i.large', 'spot', 'hibernate') returning id`,
		int64(usable.CPUMillis), usable.MemoryBytes)
	if n := launch(t, o); n != 1 {
		t.Fatalf("launched %d, want the reserve in its next pool", n)
	}
	calls := emulator.calls("RunInstances")
	if len(calls) != 2 {
		t.Fatalf("%d launches, want 2", len(calls))
	}
	f := calls[1].Form
	if f.Get("ClientToken") != host.String()+"-1" || f.Get("HibernationOptions.Configured") != "true" ||
		f.Get("InstanceMarketOptions.SpotOptions.SpotInstanceType") != "persistent" ||
		f.Get("InstanceMarketOptions.SpotOptions.InstanceInterruptionBehavior") != "hibernate" {
		t.Fatalf("next pool launch %v, want a hibernating persistent Spot request", f)
	}
}

// A refused pool answers at once: the next pool is the retry, not the
// SDK's.
// A capacity refusal cools only the zone it tried: the launch moves to the
// same type in the region's next zone.
func TestACapacityRefusalMovesToTheSameTypeInTheNextZone(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	emulator.on("RunInstances", refuseFirstPool(t, "InsufficientInstanceCapacity"))
	alice := newUser(t, o.pool, "alice@example.com")
	host := requestedHost(t, o, newWorkspace(t, o.pool, "dev", alice), `{}`)
	if n := launch(t, o); n != 1 {
		t.Fatalf("launched %d, want 1", n)
	}
	calls := emulator.calls("RunInstances")
	if len(calls) != 2 || calls[0].Form.Get("InstanceType") != calls[1].Form.Get("InstanceType") ||
		calls[0].Form.Get("SubnetId") != "subnet-east-a" || calls[1].Form.Get("SubnetId") != "subnet-east-b" {
		t.Fatalf("launches %q then %q, want the same type in us-east-2b after us-east-2a", pool(calls[0]), pool(calls[len(calls)-1]))
	}
	if zone := scan[string](t, o.pool, "select availability_zone_id from capacity_cooldowns"); zone != "use2-az1" {
		t.Fatalf("cooldown in zone %q, want only use2-az1", zone)
	}
	if zone := scan[string](t, o.pool, "select availability_zone_id from hosts where id = $1", uuid.UUID(host)); zone != "use2-az2" {
		t.Fatalf("host launched in %q, want use2-az2", zone)
	}
}

// A move counts the vCPUs live hosts hold against each quota: with 60 of
// 64 us-east-2 Spot vCPUs held elsewhere, an 8-vCPU host refused in one
// zone does not try another us-east-2 pool EC2 would refuse for quota.
func TestAMoveSkipsPoolsWhoseQuotaIsFull(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{})
	emulator.on("RunInstances", refuseFirstPool(t, "InsufficientInstanceCapacity"))
	spotPrices(t, o)
	for _, typ := range []string{"m7i.8xlarge", "m7i.4xlarge", "m7i.2xlarge", "m7i.xlarge"} {
		run(t, o.pool, `insert into hosts (name, state, kind, provider, phase, cpu_millis, memory_bytes, region, instance_type, market)
values ('busy', 'online', 'platform', 'aws', 'ready', 1000, 1::bigint << 30, 'us-east-2', $1, 'spot')`, typ)
	}
	run(t, o.pool, "insert into fleet_quotas (region, quota_class, market, vcpus, observed_at) values ('us-east-2', 'standard', 'spot', 64, now())")
	typ, _ := compute.CatalogTypeNamed("c6a.2xlarge")
	usable := typ.Usable(0)
	run(t, o.pool, `insert into hosts (name, state, kind, provider, phase, cpu_millis, memory_bytes, region, availability_zone,
    availability_zone_id, instance_type, market)
values ('r', 'offline', 'platform', 'aws', 'requested', $1, $2, 'us-east-2', 'us-east-2a', 'use2-az1', 'c6a.2xlarge', 'spot')`,
		int64(usable.CPUMillis), usable.MemoryBytes)
	launch(t, o)
	for _, c := range emulator.calls("RunInstances")[1:] {
		if strings.HasPrefix(c.Form.Get("SubnetId"), "subnet-east") {
			t.Fatalf("moved to %s, a pool over the us-east-2 Spot quota", pool(c))
		}
	}
}

// A launcher whose host another launcher moved on in the meantime neither
// records its instance nor moves the host again; the instance ends.
func TestALaunchFencedByAnotherLauncherLeavesTheHostWhereThatOnePutIt(t *testing.T) {
	for _, refuse := range []bool{false, true} {
		o, emulator, _ := launchFleet(t, compute.Fleet{})
		alice := newUser(t, o.pool, "alice@example.com")
		host := requestedHost(t, o, newWorkspace(t, o.pool, "dev", alice), `{}`)
		succeed := launched(t)
		emulator.on("RunInstances", func(call awsCall) awsReply {
			if _, err := o.pool.Exec(t.Context(), "update hosts set launch_pools = 1 where id = $1", uuid.UUID(host)); err != nil {
				t.Error(err)
			}
			if refuse {
				return ec2Error(http.StatusInternalServerError, "InsufficientInstanceCapacity", "refused")
			}
			return succeed(call)
		})
		emulator.on("TerminateInstances", terminateInstancesReply)
		if n := launch(t, o); n != 0 {
			t.Fatalf("refuse %v: launched %d for a host another launcher moved", refuse, n)
		}
		if n := len(emulator.calls("RunInstances")); n != 1 {
			t.Fatalf("refuse %v: %d launches, want the fenced one alone", refuse, n)
		}
		var phase, instanceType string
		if err := o.pool.QueryRow(t.Context(), "select phase, instance_type from hosts where id = $1", uuid.UUID(host)).
			Scan(&phase, &instanceType); err != nil {
			t.Fatal(err)
		}
		if phase != string(compute.PhaseRequested) || instanceType != emulator.calls("RunInstances")[0].Form.Get("InstanceType") {
			t.Fatalf("refuse %v: host %s as %s, want it requested where the other launcher left it", refuse, phase, instanceType)
		}
		if terminated := len(emulator.calls("TerminateInstances")); terminated != map[bool]int{false: 1, true: 0}[refuse] {
			t.Fatalf("refuse %v: %d terminations, want the fenced instance ended", refuse, terminated)
		}
	}
}

func TestAPoolRefusalIsNotRetriedBeforeTheNextPool(t *testing.T) {
	emulator := newAWS(t)
	f := emulator.fleet(compute.Fleet{})
	f.AWS.Retryer = func() aws.Retryer {
		return retry.NewStandard(func(o *retry.StandardOptions) { o.Backoff = retry.BackoffDelayerFunc(noDelay) })
	}
	o := newOwners(t, fleetConfig(f))
	publish(t, o.compute)
	emulator.on("RunInstances", refuseFirstPool(t, "InsufficientInstanceCapacity"))
	alice := newUser(t, o.pool, "alice@example.com")
	requestedHost(t, o, newWorkspace(t, o.pool, "dev", alice), `{}`)
	if n := launch(t, o); n != 1 {
		t.Fatalf("launched %d, want 1", n)
	}
	if calls := emulator.calls("RunInstances"); len(calls) != 2 {
		t.Fatalf("%d RunInstances calls, want the refused pool once and the next", len(calls))
	}
}

func noDelay(int, error) (time.Duration, error) { return 0, nil }

func TestPlatformHostsLaunchFromTheBakedNodeImageOfTheirRegion(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{Images: &compute.NodeImages{CPU: map[string]string{"us-east-2": "ami-0baked"}}})
	emulator.on("RunInstances", launched(t))
	alice := newUser(t, o.pool, "alice@example.com")
	host := requestedHost(t, o, newWorkspace(t, o.pool, "dev", alice), `{}`)
	if n := launch(t, o); n != 1 {
		t.Fatalf("launched %d, want 1", n)
	}
	calls := emulator.calls("RunInstances")
	if len(calls) != 1 || calls[0].Form.Get("ClientToken") != host.String() || calls[0].Form.Get("ImageId") != "ami-0baked" {
		t.Fatalf("RunInstances %v, want the baked us-east-2 image", calls)
	}
}

// A region the bake did not reach launches nothing rather than a stock
// image without gVisor.
func TestPlatformHostsWithoutANodeImageForTheirRegionFail(t *testing.T) {
	o, emulator, _ := launchFleet(t, compute.Fleet{Images: &compute.NodeImages{CPU: map[string]string{"us-west-2": "ami-0west"}}})
	emulator.on("RunInstances", launched(t))
	alice := newUser(t, o.pool, "alice@example.com")
	host := requestedHost(t, o, newWorkspace(t, o.pool, "dev", alice), `{}`)
	if n := launch(t, o); n != 0 {
		t.Fatalf("launched %d without a node image", n)
	}
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseFailed) {
		t.Fatalf("host without a node image is %s, want failed", phase)
	}
	if n := len(emulator.calls("RunInstances")); n != 0 {
		t.Fatalf("%d RunInstances calls for a host without an image", n)
	}
}

// A connected account launches the platform's baked image, which the
// platform shares with that account first.
func TestConnectionHostsLaunchTheBakedImageSharedWithTheirAccount(t *testing.T) {
	emulator := newAWS(t)
	o := newOwners(t, fleetConfig(emulator.fleet(compute.Fleet{
		PrincipalARN: platformPrincipal, Images: &compute.NodeImages{CPU: map[string]string{"us-east-2": "ami-0baked"}},
	})))
	publish(t, o.compute)
	customer := newCustomerAWS(emulator)
	alice := newUser(t, o.pool, "alice@example.com")
	conn := connected(t, o, customer, alice)
	ws := newWorkspace(t, o.pool, "prod", alice)
	run(t, o.pool, "update workspaces set connection_id = $1 where id = $2", conn.ID, ws)
	emulator.on("ModifyImageAttribute", func(awsCall) awsReply { return modifyImageAttributeReply() })
	emulator.on("RunInstances", launched(t))
	host := requestedHost(t, o, ws, `{}`)
	if n := launch(t, o); n != 1 {
		t.Fatalf("launched %d, want 1", n)
	}
	shares := emulator.calls("ModifyImageAttribute")
	if len(shares) != 1 || shares[0].AccessKey != platformAccessKey || shares[0].Form.Get("ImageId") != "ami-0baked" ||
		shares[0].Form.Get("LaunchPermission.Add.1.UserId") != "111111111111" {
		t.Fatalf("ModifyImageAttribute calls %v, want the platform sharing ami-0baked with 111111111111", shares)
	}
	runs := emulator.calls("RunInstances")
	if len(runs) != 1 || runs[0].Form.Get("ClientToken") != host.String() || runs[0].Form.Get("ImageId") != "ami-0baked" ||
		runs[0].AccessKey != assumedKey(conn.Active.RoleARN) {
		t.Fatalf("RunInstances calls %v, want ami-0baked launched through the connection role", runs)
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

// Between a deploy's rollout and its publish job the target still names the
// last release, whose archive the new server no longer serves; a host
// launched then fails its bootstrap. Launches wait for the served release.
func TestLaunchesWaitUntilTheTargetReleaseIsTheServedOne(t *testing.T) {
	emulator := newAWS(t)
	config := fleetConfig(emulator.fleet(compute.Fleet{}))
	config.ServedRelease = "1.1.0"
	o := newOwners(t, config)
	publish(t, o.compute)
	emulator.on("RunInstances", launched(t))
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	host := requestedHost(t, o, dev, `{}`)

	if n := launch(t, o); n != 0 || len(emulator.calls("RunInstances")) != 0 {
		t.Fatalf("launched %d to release 1.0.0 while the server serves 1.1.0", n)
	}
	if phase, _ := hostPhase(t, o.pool, host); phase != string(compute.PhaseRequested) {
		t.Fatalf("waiting host is %s, want requested", phase)
	}
	if err := o.compute.PublishAgentRelease(t.Context(), compute.AgentRelease{Version: "1.1.0", SHA256: map[string]string{
		"amd64": "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3",
	}}); err != nil {
		t.Fatal(err)
	}
	if n := launch(t, o); n != 1 {
		t.Fatalf("launched %d once the served release is the target, want 1", n)
	}
	data, err := base64.StdEncoding.DecodeString(emulator.calls("RunInstances")[0].Form.Get("UserData"))
	if err != nil || !strings.Contains(string(data), "--agent-version '1.1.0'") {
		t.Fatalf("the launch did not install the served release: %v", err)
	}
}
