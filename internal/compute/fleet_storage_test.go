package compute

import (
	"maps"
	"slices"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// readRegions are two regions, us-east-1 with two zones and us-west-2 with
// one, with Spot g6.2xlarge quotes cheapest in us-west-2, the work's 16 GB read
// from a us-east-1 bucket and the offer inputs over them.
func readRegions(t *testing.T) (OfferInputs, *VolumeRead) {
	in := offerInputs(t)
	in.Networks = map[string]Network{
		"us-east-1": {Subnets: []Subnet{{ID: "a", Zone: "us-east-1a", ZoneID: "use1-az6"}, {ID: "b", Zone: "us-east-1b", ZoneID: "use1-az1"}}},
		"us-west-2": oneZone("us-west-2a", "usw2-az2"),
	}
	for _, q := range []SpotQuote{
		{Region: "us-east-1", ZoneID: "use1-az6", HourlyMicros: 420_000},
		{Region: "us-east-1", ZoneID: "use1-az1", HourlyMicros: 440_000},
		{Region: "us-west-2", ZoneID: "usw2-az2", HourlyMicros: 340_000},
	} {
		q.InstanceType, q.ObservedAt = "g6.2xlarge", offerNow
		in.Spot = append(in.Spot, q)
	}
	return in, &VolumeRead{Region: "us-east-1", Bytes: 16_000_000_000}
}

// A read from us-east-1 ranks a us-east-1 L4 offer first, where without
// it the cheaper us-west-2 one ranks first, and a launch refused in one
// us-east-1 zone falls back to the other.
func TestReadsRankOffersInTheBucketsRegionFirst(t *testing.T) {
	t.Parallel()
	in, read := readRegions(t)
	need := Requirement{Preemptible: true, GPUs: []string{"L4"}, CPUMillis: 2000, MemoryBytes: 12 * gib}
	if offers := RankOffers(DefaultPolicy(), need, false, in); len(offers) == 0 || offers[0].Region != "us-west-2" {
		t.Fatalf("without the read, %v rank first", offerKeys(offers))
	}
	need.Read = read
	if offers := RankOffers(DefaultPolicy(), need, false, in); len(offers) == 0 || offers[0].ZoneID != "use1-az6" {
		t.Fatalf("with the read, %v rank first", offerKeys(offers))
	}
	in.Cooldowns = []OfferCooldown{{Region: "us-east-1", ZoneID: "use1-az6", InstanceType: "g6.2xlarge", Market: MarketSpot, Until: offerNow.Add(time.Hour)}}
	h := ClaimHostActionsRow{Market: ptr(string(MarketSpot)), GpuType: "L4", GpuCount: 1, CpuMillis: 2000, MemoryBytes: 12 * gib}
	waiter := HostWaitersRow{Preemptible: true, ReadRegion: &read.Region, ReadBytes: read.Bytes}
	if o, ok := fallbackPool(DefaultPolicy(), h, []HostWaitersRow{waiter}, in, func(string, bool) bool { return true }); !ok || o.ZoneID != "use1-az1" {
		t.Errorf("the fallback from use1-az6 is %s, want use1-az1", o.Key())
	}
}

// A Spot L4 reserve outside the region its market's starts read from holds
// the stopped target like any other: the market buys no replacement for it,
// whether it is ready or still preparing after its launch walk landed it
// there (prod bought one every pass on 2026-10-15 until the GPU quota
// filled). Once the market holds more than it needs, the reserve outside
// the read region retires first; each step is stable.
func TestAReserveFollowsItsMarketsReads(t *testing.T) {
	t.Parallel()
	in, read := readRegions(t)
	in.Catalog, in.StorageRegion = FleetCatalog(), read.Region
	in.ReadBytes = map[ReserveMarket]int64{{Preemptible: true, GPU: "L4"}: read.Bytes}
	reserve := func(id byte, region, zone, zoneID string, state FleetState) FleetHost {
		return FleetHost{
			ID: HostID{id}, InstanceType: "g6.2xlarge", Region: region, Zone: zone, ZoneID: zoneID, Market: MarketSpot, GPU: "L4",
			Usable: mustType(t, "g6.2xlarge").Usable(0), State: state, ReserveMode: ptr(ReserveStop), Stoppable: true, Slept: true,
			HourlyMicros: ptr(int64(400_000)), PhaseAt: offerNow,
		}
	}
	plan := func(hosts ...FleetHost) FleetPlan {
		return PlanFleet(gpuPolicy(), FleetSnapshot{Now: offerNow, Hosts: hosts, Offers: in, HostRoom: 100, ReserveRoom: 100})
	}
	walking := reserve(1, "us-west-2", "us-west-2a", "usw2-az2", FleetPreparing)
	walking.Requested = true
	if got := plan(walking).Actions; len(got) > 0 {
		t.Fatalf("while a reserve not launched yet points at us-west-2: %+v, want it awaited", got)
	}
	west := reserve(1, "us-west-2", "us-west-2a", "usw2-az2", FleetStopped)
	if got := plan(west).Actions; len(got) > 0 {
		t.Fatalf("with a ready reserve in us-west-2: %+v, want nothing bought", got)
	}
	preparing := reserve(1, "us-west-2", "us-west-2a", "usw2-az2", FleetPreparing)
	if got := plan(preparing).Actions; len(got) > 0 {
		t.Fatalf("with a reserve preparing in us-west-2: %+v, want nothing bought", got)
	}
	east := reserve(2, "us-east-1", "us-east-1a", "use1-az6", FleetStopped)
	if retired := hostsOf(actionsOf(plan(west, east), ActionRetireReserve)); !slices.Equal(retired, []HostID{west.ID}) {
		t.Fatalf("with reserves in both regions, retired %v, want the us-west-2 reserve", retired)
	}
	if got := plan(east).Actions; len(got) > 0 {
		t.Fatalf("with the us-east-1 reserve alone: %+v", got)
	}
}

// A market's read is what its recent starts read on average: one of two
// L4 starts mounting 16 GB of volumes makes it 8 GB.
func TestAMarketsReadIsItsStartsAverage(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	if _, err := pool.Exec(t.Context(), `
with ws as (insert into workspaces (name) values ('ws-' || substr(md5(random()::text), 1, 8)) returning id),
     b as (insert into workspace_buckets (workspace_id, bucket, region) select id, 'b-' || id, 'us-east-1' from ws),
     v as (insert into volumes (workspace_id, name, size_bytes) select id, 'models', 16000000000 from ws),
     app as (insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, n, s, sha256(n::text::bytea), sha256('src') from wl,
             (values (1, '{"volumes":[{"name":"models","mount_path":"/m"}]}'::jsonb), (2, '{}'::jsonb)) x(n, s)
             returning id, workload_id)
insert into containers (workspace_id, release_id, workload_id, state, slots, cpu_millis, memory_bytes, assigned_at, gpu_count, gpu_type)
select ws.id, rel.id, rel.workload_id, 'stopped', 1, 1000, 1, now(), 1, 'L4' from ws, rel`); err != nil {
		t.Fatal(err)
	}
	dbtest.OwnWorkspaces(t, pool)
	rows, err := New(pool).RecentShapes(t.Context(), recentShapesParams(DefaultPolicy()))
	if err != nil {
		t.Fatal(err)
	}
	if got := readsByMarket(rows); !maps.Equal(got, map[ReserveMarket]int64{{Preemptible: true, GPU: "L4"}: 8_000_000_000}) {
		t.Fatalf("reads %v", got)
	}
}
