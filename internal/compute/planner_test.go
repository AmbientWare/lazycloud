package compute_test

import (
	"sort"
	"sync"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// fleetNetworks are two launchable regions in the shape
// LAZYCLOUD_FLEET_NETWORKS configures.
func fleetNetworks() map[string]compute.Network {
	return map[string]compute.Network{
		"us-east-2": {VPCID: "vpc-east", SecurityGroupID: "sg-east", Subnets: []compute.Subnet{
			{ID: "subnet-east-a", Zone: "us-east-2a", ZoneID: "use2-az1"},
			{ID: "subnet-east-b", Zone: "us-east-2b", ZoneID: "use2-az2"},
		}},
		"us-west-1": {VPCID: "vpc-west", SecurityGroupID: "sg-west", Subnets: []compute.Subnet{
			{ID: "subnet-west-b", Zone: "us-west-1b", ZoneID: "usw1-az3"},
		}},
	}
}

func fleetConfig(f compute.Fleet) compute.Config {
	if f.Networks == nil {
		f.Networks = fleetNetworks()
	}
	if f.Name == "" {
		f.Name = "lazycloud-test"
	}
	f.AccountID = "111122223333"
	f.NodeRoleARN = "arn:aws:iam::111122223333:role/lazycloud-node"
	f.InstanceProfile = "lazycloud-node"
	return compute.Config{InstallURL: "https://lazycloud.test", ServerAddress: "hosts.lazycloud.test:443", Fleet: f}
}

// plan runs one fleet planning pass.
func plan(t *testing.T, o owners) compute.PlanResult {
	t.Helper()
	result, err := o.compute.Plan(t.Context(), discard())
	if err != nil {
		t.Fatalf("plan: %v", err)
	}
	return result
}

// planCapacity runs a pass and deletes the floors it bought, the hosts no
// pending container waits for, so a test sees what its demand bought. The
// fleet regions have Spot prices at 40% of on-demand.
func planCapacity(t *testing.T, o owners) compute.PlanResult {
	t.Helper()
	spotPrices(t, o)
	result := plan(t, o)
	result.Requested -= scan[int](t, o.pool, `
with floors as (
    delete from hosts h where h.phase = 'requested'
      and not exists (select 1 from containers c where c.capacity_host_id = h.id)
    returning 1
)
select count(*)::int from floors`)
	return result
}

// spotPrices quotes every catalog type in every zone of the test fleet
// networks at 40% of its on-demand price, observed now.
func spotPrices(t *testing.T, o owners) {
	t.Helper()
	var regions, zones, types []string
	var prices []int64
	for region, network := range fleetNetworks() {
		for _, s := range network.Subnets {
			for _, typ := range compute.FleetCatalog() {
				if price, sold := typ.OnDemandMicros(region); sold {
					regions, zones, types, prices = append(regions, region), append(zones, s.ZoneID), append(types, typ.Name), append(prices, price*4/10)
				}
			}
		}
	}
	run(t, o.pool, `
insert into spot_prices (region, availability_zone_id, instance_type, hourly_micros, effective_at, observed_at)
select unnest($1::text[]), unnest($2::text[]), unnest($3::text[]), unnest($4::bigint[]), now(), now()
on conflict (region, availability_zone_id, instance_type) do update set hourly_micros = excluded.hourly_micros, observed_at = now()`,
		regions, zones, types, prices)
}

// requested lists the hosts waiting for launch as
// "type market region zone gpu" strings, sorted.
func requested(t *testing.T, o owners) []string {
	t.Helper()
	rows, err := o.pool.Query(t.Context(), `
select instance_type || ' ' || market || ' ' || region || ' ' || availability_zone || ' ' || gpu_type
from hosts where phase = 'requested' order by 1`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var s string
		if err := rows.Scan(&s); err != nil {
			t.Fatal(err)
		}
		out = append(out, s)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return out
}

func capacityWait(t *testing.T, o owners, container uuid.UUID) string {
	t.Helper()
	return scan[string](t, o.pool, "select coalesce(capacity_wait, '') from containers where id = $1", container)
}

func TestCapacityBuysTheCheapestOfferEachContainerAccepts(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{}))
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	var containers []uuid.UUID
	for _, spec := range []string{
		`{}`,
		`{"placement": {"preemptible": false}}`,
		`{"resources": {"gpu": ["T4"]}}`,
		`{"placement": {"region": "us-west"}}`,
		`{"placement": {"availability_zone": "use2-az2"}}`,
		`{"placement": {"region": "eu-central"}}`,
	} {
		containers = append(containers, pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, spec), 1000, gib))
	}

	if result := planCapacity(t, o); result.Requested != 5 || result.Limited != 0 {
		t.Fatalf("result %+v, want 5 hosts requested", result)
	}
	want := []string{
		"g4dn.xlarge spot us-east-2 us-east-2a T4",
		"m7i.large on_demand us-east-2 us-east-2a ",
		"m7i.large spot us-east-2 us-east-2a ",
		"m7i.large spot us-east-2 us-east-2b ",
		"m7i.large spot us-west-1 us-west-1b ",
	}
	if got := requested(t, o); len(got) != len(want) || !equal(got, want) {
		t.Fatalf("requested hosts\n%q\nwant\n%q", got, want)
	}
	for _, c := range containers[:5] {
		if w := capacityWait(t, o, c); w != string(compute.WaitProvisioning) {
			t.Errorf("container %s waits %q, want provisioning", c, w)
		}
	}
	if w := capacityWait(t, o, containers[5]); w != "" {
		t.Errorf("a container no fleet region serves waits %q, want nothing", w)
	}
}

func equal(a, b []string) bool {
	sort.Strings(a)
	sort.Strings(b)
	for n := range a {
		if a[n] != b[n] {
			return false
		}
	}
	return len(a) == len(b)
}

func TestCapacityPacksDemandOntoOneHostAndCountsHostsInFlight(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{}))
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	release := newRelease(t, o.pool, dev, `{}`)
	// These fill the 2000 millicores of an m7i.large.
	pendingContainer(t, o.pool, dev, release, 250, 256<<20)
	pendingContainer(t, o.pool, dev, release, 1000, gib)
	pendingContainer(t, o.pool, dev, release, 250, 256<<20)
	pendingContainer(t, o.pool, dev, release, 500, 256<<20)

	if result := planCapacity(t, o); result.Requested != 1 {
		t.Fatalf("result %+v, want one host for all four", result)
	}
	if result := planCapacity(t, o); result.Requested != 0 {
		t.Fatalf("second pass requested %d more hosts for demand a host in flight covers", result.Requested)
	}
	late := pendingContainer(t, o.pool, dev, release, 1000, gib)
	if result := planCapacity(t, o); result.Requested != 1 {
		t.Fatalf("result %+v, want one more host: the host in flight is full", result)
	}
	if w := capacityWait(t, o, late); w != string(compute.WaitProvisioning) {
		t.Fatalf("late container waits %q, want provisioning", w)
	}
}

func TestConcurrentCapacityPassesBuyEachHostOnce(t *testing.T) {
	o := newOwners(t, fleetConfig(compute.Fleet{}))
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	// Each zone pin needs a host of its own.
	for _, zone := range []string{"use2-az1", "use2-az2", "usw1-az3"} {
		pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{"placement": {"availability_zone": "`+zone+`"}}`), 1000, gib)
	}

	// A pass held elsewhere skips.
	tx, err := o.pool.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(t.Context(), "select pg_advisory_xact_lock(hashtextextended('capacity', 0))"); err != nil {
		t.Fatal(err)
	}
	if result := planCapacity(t, o); !result.Skipped || result.Requested != 0 {
		t.Fatalf("pass under a held capacity lock: %+v, want skipped", result)
	}
	if err := tx.Rollback(t.Context()); err != nil {
		t.Fatal(err)
	}

	var wg sync.WaitGroup
	for range 4 {
		wg.Go(func() {
			if _, err := o.compute.Plan(t.Context(), discard()); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if n := scan[int](t, o.pool, `select count(distinct capacity_host_id) from containers c
join hosts h on h.id = c.capacity_host_id and h.phase = 'requested'`); n != 3 {
		t.Fatalf("concurrent passes bought %d hosts for the containers, want 3, one per container", n)
	}
	if result := plan(t, o); result.Requested != 0 {
		t.Fatalf("a pass after the concurrent ones %+v, want nothing more bought", result)
	}
}

func submitOne(t *testing.T, o owners, workspace uuid.UUID, app string) execution.TaskID {
	t.Helper()
	tasks, err := o.execution.Submit(t.Context(), execution.SubmitRequest{
		Workspace: identity.WorkspaceID(workspace), App: app, Function: "f",
		Inputs: []execution.TaskInput{{Payload: execution.Payload{Encoding: execution.EncodingJSON, Data: []byte(`{"args": [], "kwargs": {}}`)}}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return tasks[0].ID
}

func TestFleetLimitAndPurchasesExplainPendingTasks(t *testing.T) {
	ctx := t.Context()
	o := newOwners(t, fleetConfig(compute.Fleet{MaxHosts: 1}))
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	deploy(t, o.pool, dev, "train", `{"max_pending_tasks": 10, "resources": {"cpu_millis": 1000, "memory_mib": 1024, "gpu": ["T4"]}}`)
	deploy(t, o.pool, dev, "report", `{"max_pending_tasks": 10, "resources": {"cpu_millis": 1000, "memory_mib": 1024}}`)
	train, report := submitOne(t, o, dev, "train"), submitOne(t, o, dev, "report")
	if result, err := o.execution.Plan(ctx, discard()); err != nil || result.Created != 2 {
		t.Fatalf("plan: %+v %v, want a container per function", result, err)
	}
	place(t, o)

	// The GPU container sorts first and takes the one host the limit allows;
	// the CPU container does not run on a fleet GPU instance.
	if result := planCapacity(t, o); result.Requested != 1 || result.Limited != 1 {
		t.Fatalf("result %+v, want one host bought and one container held by the limit", result)
	}
	for task, want := range map[execution.TaskID]execution.PendingReason{
		train: execution.PendingProvisioningCompute, report: execution.PendingCapacityLimit,
	} {
		got, err := o.execution.GetTask(ctx, nil, identity.WorkspaceID(dev), task, 0)
		if err != nil {
			t.Fatal(err)
		}
		if got.Pending == nil || got.Pending.Reason != want {
			t.Errorf("task %s pending %+v, want %s", task, got.Pending, want)
		}
	}
}
