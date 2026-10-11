package compute

import (
	"slices"
	"testing"
	"time"
)

// storageReplay (testdata/fleetrun/gpu_volume.json) is a GPU function's two
// cold starts on prod, each reading its volumes from a bucket in another
// region than the hosts it got, with the Spot prices in force and the
// launches EC2 refused and filled.
type storageReplay struct {
	fleetRun
	// Arrivals are [ms after start, runtime ms].
	Arrivals     [][2]int64 `json:"arrivals"`
	VolumeBytes  int64      `json:"volume_bytes"`
	BucketRegion string     `json:"bucket_region"`
	// Refused and Filled are launches, [at, region, zone id, type, market].
	Refused [][5]string `json:"refused"`
	Filled  [][5]string `json:"filled"`
}

// storageEpisode splits the replay into its two starts' launch episodes:
// EC2 refused a pool in one and filled it in the other.
const storageEpisode = 8 * time.Minute

// replayStorage runs the replay with the planner told what each start reads,
// or not, and pools no launch tried refusing or filling. It returns the
// result and the transfer the starts paid, in µ$.
func replayStorage(t *testing.T, run storageReplay, told, untriedRefuse bool) (simResult, int64) {
	t.Helper()
	p := DefaultPolicy()
	p.Spot, p.OnDemand = MarketReserve{}, MarketReserve{}
	s := newSim(t, p)
	s.now, s.start = run.Start, run.Start
	s.in.Now, s.in.Networks = run.Start, prodNetworks()
	readTestJSON(t, "zones.json", &s.in.ZoneTypes)
	s.prices = run.quotes(t)
	s.in.Spot = s.prices(run.Start)
	outcome := map[bool]map[string]bool{false: {}, true: {}}
	record := func(launches [][5]string, filled bool) {
		for _, l := range launches {
			at, err := time.Parse(time.RFC3339, l[0])
			if err != nil {
				t.Fatal(err)
			}
			outcome[at.Before(run.Start.Add(storageEpisode))][l[1]+"/"+l[2]+"/"+l[3]+"/"+l[4]] = filled
		}
	}
	record(run.Refused, false)
	record(run.Filled, true)
	s.refuses = func(region, zoneID, instanceType string, market Market) bool {
		filled, tried := outcome[s.now.Before(run.Start.Add(storageEpisode))][region+"/"+zoneID+"/"+instanceType+"/"+string(market)]
		if tried {
			return !filled
		}
		return untriedRefuse
	}
	reads := &StorageRead{Region: run.BucketRegion, Bytes: run.VolumeBytes}
	need := Requirement{Preemptible: true, GPUs: []string{"L4", "A10G"}, CPUMillis: 2000, MemoryBytes: 12 << 30}
	if told {
		need.Storage = reads
	}
	var arrivals []simArrival
	for _, a := range run.Arrivals {
		arrivals = append(arrivals, simArrival{at: time.Duration(a[0]) * time.Millisecond, need: need, count: 1, runs: time.Duration(a[1]) * time.Millisecond, fn: "gpu"})
	}
	r := s.run(40*time.Minute, arrivals)
	if len(r.waits) != len(arrivals) || len(r.violations) > 0 {
		t.Fatalf("placed %d of %d, violations %v", len(r.waits), len(arrivals), r.violations)
	}
	var transfer int64
	for _, region := range r.placedIn {
		transfer += reads.Bytes * transferMicrosPerGB(reads.Region, region) / 1e9
	}
	return r, transfer
}

// The replayed starts cost no more, compute and transfer together, when the
// planner weighs what they read, with the pools no launch tried refusing
// and filling. They start no later but for the launches EC2 refuses in the
// pools tried first, each a pass of the launcher.
func TestFleetWeighsVolumeReadsInTheReplayedGPUStarts(t *testing.T) {
	t.Parallel()
	var run storageReplay
	readTestJSON(t, "gpu_volume.json", &run)
	for _, untriedRefuse := range []bool{true, false} {
		capacity := "untried pools fill"
		if untriedRefuse {
			capacity = "untried pools refuse"
		}
		results := map[bool]simResult{}
		totals := map[bool]int64{}
		for _, told := range []bool{false, true} {
			r, transfer := replayStorage(t, run, told, untriedRefuse)
			compute := r.spendMicros / 3600
			results[told], totals[told] = r, compute+transfer
			name := "reads unweighed"
			if told {
				name = "reads weighed"
			}
			t.Logf("%-20s %-16s compute $%.3f transfer $%.3f total $%.3f  launches %d refused %d  waits %v  regions %v",
				capacity, name, float64(compute)/1e6, float64(transfer)/1e6, float64(compute+transfer)/1e6,
				r.launches, r.refusals, r.waits, r.placedIn)
		}
		if totals[true] > totals[false] {
			t.Errorf("%s: weighing reads costs $%.3f, not weighing them $%.3f", capacity, float64(totals[true])/1e6, float64(totals[false])/1e6)
		}
		var late time.Duration
		for i, w := range results[true].waits {
			late += w - results[false].waits[i]
		}
		if extra := results[true].refusals - results[false].refusals; late > time.Duration(max(extra, 0))*simTick {
			t.Errorf("%s: weighing reads delayed the starts %s, with %d more refused launches", capacity, late, extra)
		}
	}
}

// At the replay's prices, a read from us-east-1 ranks the cheapest
// us-east-1 L4 offer before every us-west-2 one, where without it the
// cheapest us-west-2 offer ranks first, and a launch refused in us-east-2
// falls back to us-east-1.
func TestReadsRankOffersInTheBucketsRegionFirst(t *testing.T) {
	t.Parallel()
	var run storageReplay
	readTestJSON(t, "gpu_volume.json", &run)
	in := OfferInputs{Now: run.Start, Catalog: FleetCatalog(), Networks: prodNetworks(), Rates: fleetRates(t), StorageRegion: "us-east-1"}
	readTestJSON(t, "zones.json", &in.ZoneTypes)
	in.Spot = run.quotes(t)(run.Start)
	need := Requirement{Preemptible: true, GPUs: []string{"L4"}, CPUMillis: 2000, MemoryBytes: 12 << 30}
	first := func(offers []FleetOffer, region string) int {
		return slices.IndexFunc(offers, func(o FleetOffer) bool { return o.Region == region })
	}
	blind := RankOffers(DefaultPolicy(), need, false, in)
	if first(blind, "us-west-2") > first(blind, "us-east-1") {
		t.Fatalf("without the read, us-east-1 ranks first at %d", first(blind, "us-east-1"))
	}
	need.Storage = &StorageRead{Region: run.BucketRegion, Bytes: run.VolumeBytes}
	if offers := RankOffers(DefaultPolicy(), need, false, in); first(offers, "us-east-1") > first(offers, "us-west-2") {
		t.Errorf("with the read, us-west-2 ranks at %d before us-east-1 at %d", first(offers, "us-west-2"), first(offers, "us-east-1"))
	}
	for _, o := range blind {
		if o.Region == "us-east-2" {
			in.Cooldowns = append(in.Cooldowns, OfferCooldown{Region: o.Region, ZoneID: o.ZoneID, InstanceType: o.Type.Name, Market: o.Market, Until: run.Start.Add(time.Hour)})
		}
	}
	h := ClaimLaunchesRow{Market: ptr(string(MarketSpot)), GpuType: "L4", GpuCount: 1, CpuMillis: 2000, MemoryBytes: 12 << 30}
	waiter := HostWaitersRow{Preemptible: true, StorageRegion: &run.BucketRegion, StorageBytes: run.VolumeBytes}
	o, ok := fallbackPool(DefaultPolicy(), h, []HostWaitersRow{waiter}, in, func(string, bool) bool { return true })
	if !ok || o.Region != "us-east-1" {
		t.Errorf("the fallback from refused us-east-2 pools is %s, want a us-east-1 pool", o.Key())
	}
}
