package compute

import (
	"encoding/json"
	"math"
	"math/rand/v2"
	"os"
	"path/filepath"
	"slices"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

// The recorded runs (testdata/fleetrun) are two prod runs of one mixed load:
// every container as it arrived, with the shape it reserved and the
// runtime it finally needed, the fleet each run began with, the Spot price
// changes and the launches EC2 refused, and what prod recorded. Replaying
// one through PlanFleet reproduces it; the guard fails when the simulation
// drifts from what prod recorded.

// fleetRun is one recorded run.
type fleetRun struct {
	Start time.Time `json:"start"`
	// Arrivals are [ms after start, function, CPU millis, MiB, preemptible
	// 0/1, runtime ms].
	Arrivals [][6]json.RawMessage `json:"arrivals"`
	Fleet    []struct {
		Region       string    `json:"region"`
		ZoneID       string    `json:"zone_id"`
		Type         string    `json:"type"`
		Market       Market    `json:"market"`
		HourlyMicros int64     `json:"hourly_micros"`
		State        string    `json:"state"`
		Since        time.Time `json:"since"`
		ReserveMode  string    `json:"reserve_mode"`
		Protected    bool      `json:"protected"`
		FailsAt      time.Time `json:"fails_at"`
	} `json:"fleet"`
	// Refusals are [region/zone id/type, when].
	Refusals [][2]string `json:"refusals"`
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
		Reclaims       int                   `json:"reclaims"`
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

func loadFleetRun(t *testing.T, name string) fleetRun {
	t.Helper()
	var run fleetRun
	readTestJSON(t, name+".json", &run)
	return run
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
// now; extra adds pools the run did not price, such as GPU ones.
func (r fleetRun) quotes(t *testing.T, extra [][5]json.RawMessage) func(time.Time) []SpotQuote {
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
	for _, q := range slices.Concat(r.Spot, extra) {
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

// reclaimHazard is Spot reclaims per host-hour by host age, fitted to every
// recorded platform Spot host, times scale. Across both recorded runs the
// fit at scale 0.7 predicts the reclaims prod saw on the hosts it ran
// (14.1 expected, 10 seen); per run they varied far more (10 and 0).
func reclaimHazard(t *testing.T, scale float64) func(time.Duration) float64 {
	var fit struct {
		BinsMinutes []float64 `json:"bins_minutes"`
		PerHour     []float64 `json:"per_hour"`
	}
	readTestJSON(t, "hazard.json", &fit)
	return func(age time.Duration) float64 {
		i, _ := slices.BinarySearch(fit.BinsMinutes, age.Minutes())
		if i < len(fit.BinsMinutes) && fit.BinsMinutes[i] == age.Minutes() {
			i++
		}
		return scale * fit.PerHour[i]
	}
}

// prodHazardScale is the hazard scale the recorded runs support.
const prodHazardScale = 0.7

func zoneTypes(t *testing.T) map[string]map[string][]string {
	var zones map[string]map[string][]string
	readTestJSON(t, "zones.json", &zones)
	return zones
}

// newRunSim is the platform fleet as run found it: its four regions and
// zone offerings, its Spot prices, refusals and starting fleet, at prod's
// fleet limit. Reclaims are left to the caller.
func newRunSim(t *testing.T, run fleetRun, seed uint64) *sim {
	s := newSim(t, DefaultPolicy())
	s.now, s.start, s.maxHosts = run.Start, run.Start, 250
	s.in.Now, s.in.Networks, s.in.ZoneTypes = run.Start, prodNetworks(), zoneTypes(t)
	s.prices = run.quotes(t, nil)
	s.in.Spot = s.prices(run.Start)
	s.rng = rand.New(rand.NewPCG(seed, 7))
	s.recent = map[ReserveMarket]FleetCapacity{
		{Preemptible: true}: {CPUMillis: cpu.Millis(run.Recent.Spot[0]), MemoryBytes: run.Recent.Spot[1] << 20},
		{}:                  {CPUMillis: cpu.Millis(run.Recent.OnDemand[0]), MemoryBytes: run.Recent.OnDemand[1] << 20},
	}
	refused := map[string][]time.Time{}
	for _, r := range run.Refusals {
		at, err := time.Parse(time.RFC3339, r[1])
		if err != nil {
			t.Fatal(err)
		}
		refused[r[0]] = append(refused[r[0]], at)
	}
	s.refuses = func(region, zoneID, instanceType string, market Market) bool {
		return market == MarketSpot && slices.ContainsFunc(refused[region+"/"+zoneID+"/"+instanceType], func(at time.Time) bool {
			return !s.now.Before(at) && s.now.Before(at.Add(simCooldown))
		})
	}
	for _, f := range run.Fleet {
		var mode *ReserveMode
		if f.ReserveMode != "" {
			mode = ptr(ReserveMode(f.ReserveMode))
		}
		h := s.seedHost(f.Region, f.ZoneID, f.Type, f.Market, f.HourlyMicros, FleetState(f.State), mode, f.Since, f.Protected)
		if !f.FailsAt.IsZero() {
			h.failAt, h.until = ptr(f.FailsAt), f.FailsAt.Add(time.Hour)
		}
	}
	return s
}

// seedHost adds a host the run began with.
func (s *sim) seedHost(region, zoneID, typ string, market Market, hourly int64, state FleetState, mode *ReserveMode, since time.Time, protected bool) *simHost {
	t, ok := CatalogTypeNamed(typ)
	if !ok {
		s.t.Fatalf("no catalog type %s", typ)
	}
	hibernate := mode != nil && *mode == ReserveHibernate
	zone := ""
	for _, sub := range s.in.Networks[region].Subnets {
		if sub.ZoneID == zoneID {
			zone = sub.Zone
		}
	}
	o := FleetOffer{Type: t, Region: region, Zone: zone, ZoneID: zoneID, Market: market,
		Usable: t.Usable(0), HourlyMicros: hourly, StoppedMicros: rootDiskMicros(region, t.RootGiB(hibernate), t.PricedMiBps(mode != nil))}
	s.next++
	h := &simHost{offer: o, runningSince: since, FleetHost: FleetHost{
		ID: HostID{byte(s.next >> 8), byte(s.next), 0xf1}, InstanceType: typ, Region: region, Zone: zone, ZoneID: zoneID, Market: market,
		Usable: o.Usable, State: state, Current: true, HourlyMicros: ptr(hourly), ReserveMode: mode, HibernationConfigured: hibernate,
		Stoppable: mode != nil || market == MarketOnDemand, Slept: mode != nil, Protected: protected, PhaseAt: since,
	}}
	s.hosts = append(s.hosts, h)
	return h
}

// replayWindow is how long a replay runs: the run's 30 minutes of
// arrivals, its 20-minute jobs and the fleet's idle tail.
const replayWindow = 65 * time.Minute

// containerStart is how long a placed container takes to start its task,
// assign to ready on prod (2.4-2.8 s); the simulation's waits end at
// placement.
const containerStart = 2500 * time.Millisecond

// TestFleetReplayMatchesProd replays run 2, which saw no reclaims, without
// them, and holds the simulation to what prod recorded: revenue, the fleet
// CPU-hours and spend above the idle rate in the recorded window, the hosts
// launched, and each function's start p50 and p95.
func TestFleetReplayMatchesProd(t *testing.T) {
	t.Parallel()
	run := loadFleetRun(t, "run2")
	s := newRunSim(t, run, 1)
	window := time.Duration(run.Recorded.WindowSeconds) * time.Second
	s.until = run.Start.Add(window)
	r := s.run(replayWindow, run.arrivals(t))
	rec := run.Recorded
	revenue, fleetCPU := float64(r.revenueNanos)/1e9, r.fleetCPUSec/3600
	cost := float64(r.windowMicros-r.refundMicros)/3600e6 - rec.IdleRate*window.Hours()
	t.Logf("run 2: revenue $%.2f (prod $%.2f), fleet %.1f CPU-h (%.1f), cost above idle $%.2f ($%.2f), hosts launched %d (%d)",
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
