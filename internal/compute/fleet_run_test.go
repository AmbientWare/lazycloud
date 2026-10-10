package compute

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

// The recorded run (testdata/fleetrun) is a prod run of a mixed load: every
// container as it arrived, with the shape it reserved and the runtime it
// finally needed, the fleet the run began with, the Spot price changes, and
// what prod recorded. Replaying it through PlanFleet reproduces it; the guard
// fails when the simulation drifts from what prod recorded.

// fleetRun is one recorded run.
type fleetRun struct {
	Start time.Time `json:"start"`
	// Arrivals are [ms after start, function, CPU millis, MiB, preemptible
	// 0/1, runtime ms].
	Arrivals [][6]json.RawMessage `json:"arrivals"`
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
	Recorded struct {
		Revenue        float64               `json:"revenue"`
		FleetCPUHours  float64               `json:"fleet_cpu_hours"`
		CostAboveIdle  float64               `json:"cost_above_idle"`
		IdleRate       float64               `json:"idle_rate"`
		WindowSeconds  int                   `json:"window_seconds"`
		HostsLaunched  int                   `json:"hosts_launched"`
		WaitsP50AndP95 map[string][2]float64 `json:"waits_p50_p95"`
	} `json:"recorded"`
	// Prices are every Spot price change, [region, zone id, type, µ$/h,
	// effective]; Spot is the latest quote per pool with its placement
	// score, [region, zone id, type, µ$/h, score].
	Prices [][5]json.RawMessage `json:"prices"`
	Spot   [][5]json.RawMessage `json:"spot"`
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
		var fn string
		unmarshalAll(t, a[:], &at, &fn, &cpuMillis, &mib, &preemptible, &runs)
		out = append(out, simArrival{
			at: time.Duration(at) * time.Millisecond, count: 1, runs: time.Duration(runs) * time.Millisecond, fn: fn,
			need: Requirement{CPUMillis: cpu.Millis(cpuMillis), MemoryBytes: mib << 20, Preemptible: preemptible == 1},
		})
	}
	return out
}

// quotes are the run's Spot quotes, each at its pool's price in force at
// now.
func (r fleetRun) quotes(t *testing.T) func(time.Time) []SpotQuote {
	type change struct {
		at     time.Time
		micros int64
	}
	history := map[string][]change{}
	for _, p := range r.Prices {
		var region, zone, typ, at string
		var micros int64
		unmarshalAll(t, p[:], &region, &zone, &typ, &micros, &at)
		when, err := time.Parse(time.RFC3339, at)
		if err != nil {
			t.Fatal(err)
		}
		history[region+"/"+zone+"/"+typ] = append(history[region+"/"+zone+"/"+typ], change{when, micros})
	}
	for _, h := range history {
		slices.SortFunc(h, func(a, b change) int { return a.at.Compare(b.at) })
	}
	var base []SpotQuote
	for _, q := range r.Spot {
		var sq SpotQuote
		unmarshalAll(t, q[:], &sq.Region, &sq.ZoneID, &sq.InstanceType, &sq.HourlyMicros, &sq.PlacementScore)
		base = append(base, sq)
	}
	return func(now time.Time) []SpotQuote {
		out := slices.Clone(base)
		for i, q := range out {
			out[i].ObservedAt = now
			for _, c := range history[q.Region+"/"+q.ZoneID+"/"+q.InstanceType] {
				if c.at.After(now) {
					break
				}
				out[i].HourlyMicros = c.micros
			}
		}
		return out
	}
}

// newRunSim is the platform fleet as run found it: its four regions and
// zone offerings, its Spot prices and starting fleet, at prod's fleet limit.
func newRunSim(t *testing.T, run fleetRun) *sim {
	s := newSim(t, DefaultPolicy())
	s.now, s.start, s.maxHosts = run.Start, run.Start, 250
	s.in.Now, s.in.Networks = run.Start, prodNetworks()
	readTestJSON(t, "zones.json", &s.in.ZoneTypes)
	s.prices = run.quotes(t)
	s.in.Spot = s.prices(run.Start)
	s.recent = map[ReserveMarket]FleetCapacity{
		{Preemptible: true}: {CPUMillis: cpu.Millis(run.Recent.Spot[0]), MemoryBytes: run.Recent.Spot[1] << 20},
		{}:                  {CPUMillis: cpu.Millis(run.Recent.OnDemand[0]), MemoryBytes: run.Recent.OnDemand[1] << 20},
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
			Usable: o.Usable, State: f.State, Current: true, HourlyMicros: ptr(f.HourlyMicros), ReserveMode: f.ReserveMode, HibernationConfigured: hibernate,
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

// replayWindow is how long a replay runs: the run's 30 minutes of
// arrivals, its 20-minute jobs and the fleet's idle tail.
const replayWindow = 65 * time.Minute

// containerStart is how long a placed container takes to start its task,
// assign to ready on prod (2.4-2.8 s); the simulation's waits end at
// placement.
const containerStart = 2500 * time.Millisecond

// TestFleetReplayMatchesProd replays the run, which saw no reclaims, without
// them, and holds the simulation to what prod recorded: revenue, the fleet
// CPU-hours and spend above the idle rate in the recorded window, the hosts
// launched, and each function's start p50 and p95.
func TestFleetReplayMatchesProd(t *testing.T) {
	t.Parallel()
	var run fleetRun
	readTestJSON(t, "run2.json", &run)
	s := newRunSim(t, run)
	// The policy prod ran when it recorded the run: Spot hosts idled as
	// long as on-demand ones.
	s.p.SpotIdleTimeout = s.p.IdleTimeout
	window := time.Duration(run.Recorded.WindowSeconds) * time.Second
	s.until = run.Start.Add(window)
	r := s.run(replayWindow, run.arrivals(t))
	rec := run.Recorded
	revenue, fleetCPU := float64(r.revenueNanos)/1e9, r.fleetCPUSec/3600
	cost := float64(r.windowMicros)/3600e6 - rec.IdleRate*window.Hours()
	t.Logf("revenue $%.2f (prod $%.2f), fleet %.1f CPU-h (%.1f), cost above idle $%.2f ($%.2f), hosts launched %d (%d)",
		revenue, rec.Revenue, fleetCPU, rec.FleetCPUHours, cost, rec.CostAboveIdle, r.launches, rec.HostsLaunched)
	within := func(what string, got, want, tolerance float64) {
		if math.Abs(got-want) > tolerance*want {
			t.Errorf("%s %.2f, prod recorded %.2f: more than %.0f%% apart", what, got, want, 100*tolerance)
		}
	}
	// The recorded spend also paid for an agent refresh of two on-demand
	// reserves and two Spot hosts whose bootstraps timed out (about $0.08 and
	// 2.7 CPU-hours), which the starting fleet does not refresh.
	within("revenue", revenue, rec.Revenue, 0.02)
	within("fleet CPU-hours", fleetCPU, rec.FleetCPUHours, 0.08)
	within("cost above idle", cost, rec.CostAboveIdle, 0.10)
	if d := r.launches - rec.HostsLaunched; d < -3 || d > 3 {
		t.Errorf("launched %d hosts, prod %d", r.launches, rec.HostsLaunched)
	}
	if len(s.pending) > 0 || len(r.violations) > 0 {
		t.Errorf("%d containers unplaced, violations %v", len(s.pending), r.violations)
	}
	for fn, want := range rec.WaitsP50AndP95 {
		for i, q := range []float64{0.5, 0.95} {
			got := (quantile(r.fnWaits[fn], q) + containerStart).Seconds()
			if math.Abs(got-want[i]) > 4 {
				t.Errorf("%s start p%.0f %.1fs, prod %.1fs", fn, 100*q, got, want[i])
			}
		}
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
