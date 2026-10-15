package compute

import (
	"encoding/json"
	"maps"
	"math"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

// A recorded run (testdata/fleetrun) is a prod run of a mixed load: every
// container as it arrived, with the shape it reserved and the runtime it
// finally needed, the fleet the run began with, the Spot quotes, the
// capacity refusals EC2 made, and what prod recorded. Replaying it through
// PlanFleet checks that every container runs and placement holds, and
// reports what the run cost beside what prod recorded.

// fleetRun is one recorded run.
type fleetRun struct {
	Start time.Time `json:"start"`
	// Arrivals are [ms after start, function, CPU millis, MiB, preemptible
	// 0/1, runtime ms], and the GPU model of GPU work.
	Arrivals [][]json.RawMessage `json:"arrivals"`
	Fleet    []struct {
		Region       string       `json:"region"`
		ZoneID       string       `json:"zone_id"`
		Type         string       `json:"type"`
		Market       Market       `json:"market"`
		HourlyMicros int64        `json:"hourly_micros"`
		State        FleetState   `json:"state"`
		Since        time.Time    `json:"since"`
		ReserveMode  *ReserveMode `json:"reserve_mode"`
		FailsAt      *time.Time   `json:"fails_at"`
	} `json:"fleet"`
	// Recent are the largest shapes each CPU market placed within the
	// LargestShape window, [CPU millis, MiB].
	Recent struct {
		Spot     [2]int64 `json:"spot"`
		OnDemand [2]int64 `json:"on_demand"`
	} `json:"recent"`
	// RecentGPU are the largest shapes GPU markets placed, [preemptible 0/1,
	// model, CPU millis, MiB].
	RecentGPU [][4]json.RawMessage `json:"recent_gpu"`
	// Refusals are EC2's capacity refusals of the run's launches, [ms after
	// start, region, zone id, type, market].
	Refusals [][5]json.RawMessage `json:"refusals"`
	Recorded struct {
		HostsLaunched  int                   `json:"hosts_launched"`
		WaitsP50AndP95 map[string][2]float64 `json:"waits_p50_p95"`
		Refused        int                   `json:"refused"`
		// NeverRanSpotCPU counts the Spot CPU hosts that served and ended
		// without running a container.
		NeverRanSpotCPU int `json:"never_ran_spot_cpu"`
	} `json:"recorded"`
	// Spot is the latest quote per pool with its placement score, [region,
	// zone id, type, µ$/h, score].
	Spot [][5]json.RawMessage `json:"spot"`
}

func readTestJSON(t *testing.T, name string, v any) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", "fleetrun", name))
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, v); err != nil {
		t.Fatalf("%s: %v", name, err)
	}
}

func unmarshalAll(t *testing.T, raws []json.RawMessage, vs ...any) {
	t.Helper()
	for i, v := range vs {
		if err := json.Unmarshal(raws[i], v); err != nil {
			t.Fatal(err)
		}
	}
}

// arrivals are the run's containers as the simulation takes them.
func (r fleetRun) arrivals(t *testing.T) []simArrival {
	out := make([]simArrival, 0, len(r.Arrivals))
	for _, a := range r.Arrivals {
		var at, cpuMillis, mib, preemptible, runs int64
		var fn, gpu string
		unmarshalAll(t, a[:6], &at, &fn, &cpuMillis, &mib, &preemptible, &runs)
		if len(a) > 6 {
			unmarshalAll(t, a[6:], &gpu)
		}
		need := Requirement{CPUMillis: cpu.Millis(cpuMillis), MemoryBytes: mib << 20, Preemptible: preemptible == 1}
		if gpu != "" {
			need.GPUs, need.GPUCount = []string{gpu}, 1
		}
		out = append(out, simArrival{at: time.Duration(at) * time.Millisecond, count: 1, runs: time.Duration(runs) * time.Millisecond, fn: fn, need: need})
	}
	return out
}

// refuses is EC2 refusing a pool at the sim's time as the run recorded:
// within five minutes of a recorded refusal of its type and market in its
// region. EC2 refused prod's walks zone after zone, so a zone the run never
// tried refuses with the rest.
func (r fleetRun) refuses(t *testing.T, s *sim) func(region, zoneID, instanceType string, market Market) bool {
	refused := map[string][]time.Time{}
	for _, f := range r.Refusals {
		var at int64
		var region, zone, typ, market string
		unmarshalAll(t, f[:], &at, &region, &zone, &typ, &market)
		key := region + "/" + typ + "/" + market
		refused[key] = append(refused[key], r.Start.Add(time.Duration(at)*time.Millisecond))
	}
	return func(region, zoneID, instanceType string, market Market) bool {
		return slices.ContainsFunc(refused[region+"/"+instanceType+"/"+string(market)], func(at time.Time) bool {
			return s.now.Sub(at).Abs() <= 5*time.Minute
		})
	}
}

// quotes are the run's Spot quotes, observed at now.
func (r fleetRun) quotes(t *testing.T) func(time.Time) []SpotQuote {
	var base []SpotQuote
	for _, q := range r.Spot {
		var sq SpotQuote
		unmarshalAll(t, q[:], &sq.Region, &sq.ZoneID, &sq.InstanceType, &sq.HourlyMicros, &sq.PlacementScore)
		base = append(base, sq)
	}
	return func(now time.Time) []SpotQuote {
		out := slices.Clone(base)
		for i := range out {
			out[i].ObservedAt = now
		}
		return out
	}
}

// newRunSim is the platform fleet as run found it: its four regions and
// zone offerings, its Spot quotes and starting fleet, at prod's fleet limit.
func newRunSim(t *testing.T, run fleetRun) *sim {
	p := DefaultPolicy()
	s := &sim{
		t: t, p: p, now: run.Start, start: run.Start, maxHosts: 250, provision: p.Starts.Launch,
		in: OfferInputs{Now: run.Start, Catalog: FleetCatalog(), Networks: prodNetworks(), Rates: fleetRates(t), StorageRegion: "us-east-1"},
	}
	readTestJSON(t, "zones.json", &s.in.ZoneTypes)
	s.prices = run.quotes(t)
	s.in.Spot = s.prices(run.Start)
	s.recent = map[ReserveMarket]FleetCapacity{
		{Preemptible: true}: {CPUMillis: cpu.Millis(run.Recent.Spot[0]), MemoryBytes: run.Recent.Spot[1] << 20},
		{}:                  {CPUMillis: cpu.Millis(run.Recent.OnDemand[0]), MemoryBytes: run.Recent.OnDemand[1] << 20},
	}
	for _, g := range run.RecentGPU {
		var preemptible, cpuMillis, mib int64
		var model string
		unmarshalAll(t, g[:], &preemptible, &model, &cpuMillis, &mib)
		s.recent[ReserveMarket{Preemptible: preemptible == 1, GPU: model}] = FleetCapacity{CPUMillis: cpu.Millis(cpuMillis), MemoryBytes: mib << 20, GPUs: 1}
	}
	for _, f := range run.Fleet {
		typ, ok := CatalogTypeNamed(f.Type)
		if !ok {
			t.Fatalf("no catalog type %s", f.Type)
		}
		slept := f.ReserveMode != nil
		hibernate := slept && *f.ReserveMode == ReserveHibernate
		zone := ""
		for _, sub := range s.in.Networks[f.Region].Subnets {
			if sub.ZoneID == f.ZoneID {
				zone = sub.Zone
			}
		}
		o := FleetOffer{Type: typ, Region: f.Region, Zone: zone, ZoneID: f.ZoneID, Market: f.Market, Usable: typ.Usable(0),
			HourlyMicros: f.HourlyMicros, StoppedMicros: rootDiskMicros(f.Region, typ.RootGiB(hibernate), typ.PricedMiBps(slept))}
		s.next++
		h := &simHost{offer: o, failAt: f.FailsAt, FleetHost: FleetHost{
			ID: HostID{byte(s.next >> 8), byte(s.next), 0xf1}, InstanceType: f.Type, Region: f.Region, Zone: zone, ZoneID: f.ZoneID, Market: f.Market,
			GPU: typ.GPU, Usable: o.Usable, State: f.State, HourlyMicros: ptr(f.HourlyMicros), ReserveMode: f.ReserveMode, HibernationConfigured: hibernate,
			Stoppable: slept || f.Market == MarketOnDemand, Slept: slept, PhaseAt: f.Since,
		}}
		if f.FailsAt != nil {
			// It holds its phase until it fails.
			h.until = f.FailsAt.Add(time.Hour)
		}
		s.hosts = append(s.hosts, h)
	}
	return s
}

// TestFleetReplayOfAMixedGPURun replays gpu_refusals.json, a mixed CPU load
// beside on-demand A10G functions while EC2 refused GPU pools: every
// container runs and nothing breaks placement. It reports hosts launched,
// refusals, spend, Spot CPU hosts that never ran a container, and each
// function's start p50 and p95 beside what prod recorded.
func TestFleetReplayOfAMixedGPURun(t *testing.T) {
	t.Parallel()
	var run fleetRun
	readTestJSON(t, "gpu_refusals.json", &run)
	s := newRunSim(t, run)
	s.refuses = run.refuses(t, s)
	// Launches of on-demand g5 hosts served in about a minute (00:11:58 to
	// 00:13:00 and 00:29:02 to 00:29:44).
	s.gpuProvision = time.Minute
	// The run began with the Spot L4 and A10G pools cooling after a walk
	// EC2 refused at 23:51, for the 40 minutes its pools had been refused
	// before; the planner bought there again at 00:31.
	for _, region := range slices.Sorted(maps.Keys(s.in.Networks)) {
		for _, typ := range s.in.Catalog {
			if typ.GPU == "L4" || typ.GPU == "A10G" {
				s.cool(OfferCooldown{Region: region, InstanceType: typ.Name, Market: MarketSpot}, nil)
				s.cools[OfferCooldown{Region: region, InstanceType: typ.Name, Market: MarketSpot}].until = run.Start.Add(30 * time.Minute)
			}
		}
	}
	s.until = run.Start.Add(50 * time.Minute)
	r := s.run(50*time.Minute, run.arrivals(t))
	rec := run.Recorded
	t.Logf("hosts launched %d (prod %d), refused %d (%d), spend $%.2f, Spot CPU hosts that never ran a container %d (%d)",
		r.launches, rec.HostsLaunched, r.refusals, rec.Refused, float64(r.windowMicros)/3600e6, r.unusedSpotCPU, rec.NeverRanSpotCPU)
	for fn, want := range rec.WaitsP50AndP95 {
		for i, q := range []float64{0.5, 0.95} {
			t.Logf("%s start p%.0f %.1fs (prod %.1fs)", fn, 100*q, quantile(r.fnWaits[fn], q).Seconds(), want[i])
		}
	}
	t.Logf("actions %v", r.actions)
	if len(s.pending) > 0 || len(r.violations) > 0 {
		t.Errorf("%d containers unplaced, violations %v", len(s.pending), r.violations)
	}
}

func quantile(ds []time.Duration, q float64) time.Duration {
	if len(ds) == 0 {
		return 0
	}
	sorted := slices.Clone(ds)
	slices.Sort(sorted)
	return sorted[max(int(math.Ceil(q*float64(len(sorted))))-1, 0)]
}
