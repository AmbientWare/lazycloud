package compute_test

import (
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/compute"
)

// constrained places one pending container per spec and returns where each
// went, nil for still pending.
func constrained(t *testing.T, o owners, workspace uuid.UUID, specs ...string) []*uuid.UUID {
	t.Helper()
	ids := make([]uuid.UUID, len(specs))
	for n, spec := range specs {
		ids[n] = pendingContainer(t, o.pool, workspace, newRelease(t, o.pool, workspace, spec), 1000, gib)
	}
	place(t, o)
	out := make([]*uuid.UUID, len(ids))
	for n, id := range ids {
		out[n] = containerHost(t, o.pool, id)
	}
	return out
}

func on(got *uuid.UUID, host compute.HostID) bool { return got != nil && *got == uuid.UUID(host) }

func TestGPUWorkTakesAnAcceptedModelAndCountsReservedGPUs(t *testing.T) {
	o := newOwners(t, compute.Config{})
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	a10 := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, CPU: 16000, Memory: 64 * gib, GPUType: "A10G", GPUCount: 1})
	t4 := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, CPU: 16000, Memory: 64 * gib, GPUType: "T4", GPUCount: 4})
	// A live container naming a model without a count reserves one T4.
	held := newRelease(t, o.pool, dev, `{"resources": {"gpu": ["T4"]}}`)
	run(t, o.pool, `insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
values ($1, $2, 'ready', $3, 1, 1000, 1 << 30, now())`, dev, held, uuid.UUID(t4))

	got := constrained(t, o, dev,
		`{"resources": {"gpu": ["L4", "T4"], "gpu_count": 2}}`,
		`{"resources": {"gpu": ["H100"]}}`,
		`{"resources": {"gpu": ["A10G"]}}`,
	)
	if !on(got[0], t4) {
		t.Errorf("two of L4 or T4 went to %v, want the T4 host", got[0])
	}
	if got[1] != nil {
		t.Errorf("H100 work went to %v; no host has that model", got[1])
	}
	if !on(got[2], a10) {
		t.Errorf("A10G work went to %v, want the A10G host", got[2])
	}
	// One T4 is left: two more GPUs do not fit, one does, and the A10G host
	// is full.
	got = constrained(t, o, dev,
		`{"resources": {"gpu": ["T4"], "gpu_count": 2}}`,
		`{"resources": {"gpu": ["any"]}}`,
		`{"resources": {"gpu": ["any"]}}`,
	)
	if got[0] != nil {
		t.Errorf("two T4s went to %v with one free", got[0])
	}
	if (got[1] == nil) == (got[2] == nil) || (!on(got[1], t4) && !on(got[2], t4)) {
		t.Errorf("any-GPU work went to %v and %v, want exactly one on the last free T4", got[1], got[2])
	}
}

func TestCPUWorkStaysOffFleetGPUInstancesButRunsOnGPUMachines(t *testing.T) {
	o := newOwners(t, machineConfig())
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, CPU: 16000, Memory: 64 * gib, GPUType: "L4", GPUCount: 1})
	publish(t, o.compute)
	cmd, err := o.compute.CreateMachineJoin(t.Context(), compute.MachineJoin{Account: alice, Name: "gpu-box", Workspaces: []uuid.UUID{dev}})
	if err != nil {
		t.Fatal(err)
	}
	gpu := compute.Capacity{CPUMillis: 8000, MemoryBytes: 16 * gib, GPUType: "A100-80", GPUCount: 2}
	machine, _, err := o.compute.Enroll(t.Context(), joinToken(t, cmd), compute.HostReport{Hostname: "gpu-box", Capacity: gpu})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := o.compute.OpenSession(t.Context(), machine, compute.SessionOpen{BootID: "boot", Capacity: gpu}); err != nil {
		t.Fatal(err)
	}

	got := constrained(t, o, dev, `{}`, `{"placement": {"machine": "gpu-box"}}`)
	if got[0] != nil {
		t.Errorf("CPU work went to %v; the only platform host is a fleet GPU instance", got[0])
	}
	if !on(got[1], machine) {
		t.Errorf("CPU work pinned to a GPU machine went to %v, want the machine", got[1])
	}
	operator := newHost(t, o.pool, hostSpec{GPUType: "T4", GPUCount: 1})
	if got := constrained(t, o, dev, `{}`); !on(got[0], operator) {
		t.Errorf("CPU work went to %v, want the operator's GPU host, which runs what it is sent", got[0])
	}
}

func TestAssignmentRecordsWhoseMachineAndWhichGPUBillingPrices(t *testing.T) {
	o := newOwners(t, machineConfig())
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	fleet := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, CPU: 16000, Memory: 64 * gib, GPUType: "L4", GPUCount: 1})
	publish(t, o.compute)
	cmd, err := o.compute.CreateMachineJoin(t.Context(), compute.MachineJoin{Account: alice, Name: "gpu-box", Workspaces: []uuid.UUID{dev}})
	if err != nil {
		t.Fatal(err)
	}
	gpu := compute.Capacity{CPUMillis: 8000, MemoryBytes: 16 * gib, GPUType: "A100-80", GPUCount: 2}
	machine, _, err := o.compute.Enroll(t.Context(), joinToken(t, cmd), compute.HostReport{Hostname: "gpu-box", Capacity: gpu})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := o.compute.OpenSession(t.Context(), machine, compute.SessionOpen{BootID: "boot", Capacity: gpu}); err != nil {
		t.Fatal(err)
	}
	onFleet := pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{"resources": {"gpu": ["L4"]}}`), 1000, gib)
	onMachine := pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{"resources": {"gpu": ["A100-80"]}, "placement": {"machine": "gpu-box"}}`), 1000, gib)
	cpuOnMachine := pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{"placement": {"machine": "gpu-box"}}`), 1000, gib)
	run(t, o.pool, "update containers set gpu_count = 1 where id = any($1)", []uuid.UUID{onFleet, onMachine})
	place(t, o)
	for _, want := range []struct {
		container uuid.UUID
		host      compute.HostID
		gpu       string
		owner     string
	}{
		{onFleet, fleet, "L4", "platform_fleet"},
		{onMachine, machine, "A100-80", "self_hosted"},
		{cpuOnMachine, machine, "", "self_hosted"},
	} {
		var gpuType, owner string
		if err := o.pool.QueryRow(t.Context(), "select gpu_type, billing_owner from containers where id = $1", want.container).Scan(&gpuType, &owner); err != nil {
			t.Fatal(err)
		}
		if h := containerHost(t, o.pool, want.container); !on(h, want.host) || gpuType != want.gpu || owner != want.owner {
			t.Errorf("container on %v records GPU %q and owner %s, want %q and %s", h, gpuType, owner, want.gpu, want.owner)
		}
	}
}

func TestNonPreemptibleWorkNeverRunsOnSpot(t *testing.T) {
	o := newOwners(t, compute.Config{})
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	spot := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Market: compute.MarketSpot, Region: "us-east-2"})
	strict := pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{"placement": {"preemptible": false}}`), 1000, gib)
	loose := pendingContainer(t, o.pool, dev, newRelease(t, o.pool, dev, `{}`), 1000, gib)

	place(t, o)
	if h := containerHost(t, o.pool, strict); h != nil {
		t.Fatalf("non-preemptible work went to %v, a spot host", h)
	}
	if h := containerHost(t, o.pool, loose); !on(h, spot) {
		t.Fatalf("preemptible work went to %v, want the spot host", h)
	}
	onDemand := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Market: compute.MarketOnDemand, Region: "us-east-2"})
	place(t, o)
	if h := containerHost(t, o.pool, strict); !on(h, onDemand) {
		t.Fatalf("non-preemptible work went to %v, want the on-demand host", h)
	}
}

func TestRegionAndZonePinsChooseTheHost(t *testing.T) {
	o := newOwners(t, compute.Config{})
	alice := newUser(t, o.pool, "alice@example.com")
	dev := newWorkspace(t, o.pool, "dev", alice)
	east := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Market: compute.MarketOnDemand, CPU: 16000, Memory: 64 * gib,
		Region: "us-east-2", Zone: "us-east-2a", ZoneID: "use2-az1"})
	west := newHost(t, o.pool, hostSpec{Provider: compute.ProviderAWS, Market: compute.MarketOnDemand, CPU: 16000, Memory: 64 * gib,
		Region: "us-west-1", Zone: "us-west-1b", ZoneID: "usw1-az3"})

	got := constrained(t, o, dev,
		`{"placement": {"region": "us-west"}}`,
		`{"placement": {"availability_zone": "use2-az1"}}`,
		`{"placement": {"availability_zone": "us-west-1b"}}`,
		`{"placement": {"region": "us-east", "availability_zone": "us-west-1b"}}`,
		`{"placement": {"region": "eu-central"}}`,
	)
	if !on(got[0], west) {
		t.Errorf("us-west work went to %v, want the us-west-1 host", got[0])
	}
	if !on(got[1], east) {
		t.Errorf("work pinned to zone id use2-az1 went to %v, want the us-east-2a host", got[1])
	}
	if !on(got[2], west) {
		t.Errorf("work pinned to zone us-west-1b went to %v, want the us-west-1 host", got[2])
	}
	if got[3] != nil || got[4] != nil {
		t.Errorf("work whose region and zone no host matches went to %v and %v", got[3], got[4])
	}
}
