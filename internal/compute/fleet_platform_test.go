package compute

import (
	"cmp"
	"encoding/json"
	"fmt"
	"maps"
	"math"
	"math/rand/v2"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

// The platform scenarios run the calibrated simulation across the whole
// platform fleet: four US regions with their recorded vCPU quotas and
// refused pools, Spot and on-demand CPU, the T4, A10G and L4 markets,
// work that cannot run on Spot, region-pinned work, image builds, a
// connected account, and hibernating and stopped reserves, under the
// recorded run's load and at up to twenty times it. Each prints one row
// per scenario, averaged over seeds, and asserts invariants.

// recordedQuotas are the platform's vCPU quotas as Service Quotas reported
// them on 2026-10-09.
func recordedQuotas() map[QuotaKey]int64 {
	quotas := map[QuotaKey]int64{}
	for _, region := range []string{"us-east-1", "us-east-2", "us-west-1", "us-west-2"} {
		quotas[QuotaKey{Region: region, Class: QuotaStandard, Market: MarketSpot}] = 512
		quotas[QuotaKey{Region: region, Class: QuotaStandard, Market: MarketOnDemand}] = 512
		quotas[QuotaKey{Region: region, Class: QuotaG, Market: MarketOnDemand}] = 384
		quotas[QuotaKey{Region: region, Class: QuotaG, Market: MarketSpot}] = 128
	}
	quotas[QuotaKey{Region: "us-east-2", Class: QuotaG, Market: MarketOnDemand}] = 256
	quotas[QuotaKey{Region: "us-east-2", Class: QuotaG, Market: MarketSpot}] = 256
	return quotas
}

// platformScenario is a load and the fleet it runs on.
type platformScenario struct {
	name   string
	window time.Duration
	seeds  int
	// arrivals are the scenario's work; setup adjusts the run's fleet.
	arrivals func(t *testing.T, run fleetRun) []simArrival
	setup    func(t *testing.T, s *sim)
}

// steadyLoad is run's work per hour arriving as many independent customers'
// would: each function's containers at its average rate over the run's 30
// minutes, scale times it, as Poisson arrivals over d, each running a
// duration drawn from the function's recorded ones.
func steadyLoad(scale float64, d time.Duration) func(*testing.T, fleetRun) []simArrival {
	return func(t *testing.T, run fleetRun) []simArrival {
		rng := rand.New(rand.NewPCG(42, 1))
		byFn := map[string][]simArrival{}
		for _, a := range run.arrivals(t) {
			byFn[a.fn] = append(byFn[a.fn], a)
		}
		var out []simArrival
		for _, fn := range slices.Sorted(maps.Keys(byFn)) {
			recorded := byFn[fn]
			perSecond := scale * float64(len(recorded)) / (30 * 60)
			for at := rng.ExpFloat64() / perSecond; at < d.Seconds(); at += rng.ExpFloat64() / perSecond {
				a := recorded[rng.IntN(len(recorded))]
				a.at = time.Duration(at * float64(time.Second))
				out = append(out, a)
			}
		}
		return out
	}
}

// recurringBursts is 100 ten-minute Spot starts every 25 minutes, beside
// run's pinned work.
func recurringBursts(t *testing.T, run fleetRun) []simArrival {
	var out []simArrival
	for i := range 3 {
		out = append(out, simArrival{at: time.Duration(i) * 25 * time.Minute, need: Requirement{CPUMillis: 1000, MemoryBytes: 2 * gib, Preemptible: true},
			count: 100, runs: 10 * time.Minute, fn: "burst"})
	}
	for _, a := range run.arrivals(t) {
		if a.fn == "pinned" {
			out = append(out, a)
		}
	}
	return out
}

// gpuMix is GPU work of each fleet model beside steady CPU work: T4
// inference that may run on Spot, A10G jobs that may not, and bursts of
// short L4 jobs.
func gpuMix(t *testing.T, run fleetRun) []simArrival {
	gpu := func(model string, cpus cpu.Millis, memGiB int64, preemptible bool) Requirement {
		return Requirement{GPUs: []string{model}, GPUCount: 1, CPUMillis: cpus, MemoryBytes: memGiB * gib, Preemptible: preemptible}
	}
	out := steadyLoad(1, 60*time.Minute)(t, run)
	rng := rand.New(rand.NewPCG(43, 1))
	for at := 0.0; at < 3600; at += rng.ExpFloat64() * 120 {
		out = append(out, simArrival{at: time.Duration(at * float64(time.Second)), need: gpu("T4", 4000, 16, true), count: 1, runs: 5 * time.Minute, fn: "t4"})
	}
	for at := 0.0; at < 3600; at += rng.ExpFloat64() * 600 {
		out = append(out, simArrival{at: time.Duration(at * float64(time.Second)), need: gpu("A10G", 8000, 32, false), count: 1, runs: 15 * time.Minute, fn: "a10g"})
	}
	for _, at := range []time.Duration{5 * time.Minute, 35 * time.Minute} {
		out = append(out, simArrival{at: at, need: gpu("L4", 4000, 16, true), count: 6, runs: 2 * time.Minute, fn: "l4"})
	}
	return out
}

// manyCustomers is forty customers' functions of mixed shapes, 0.25 to 8
// CPUs at 1 to 8 GiB a CPU running seconds to half an hour, each arriving
// steadily or in bursts: a fifth cannot run on Spot, some are pinned to a
// region and some are image builds.
func manyCustomers(t *testing.T, _ fleetRun) []simArrival {
	rng := rand.New(rand.NewPCG(44, 1))
	var out []simArrival
	for c := range 40 {
		cpus := []cpu.Millis{250, 500, 1000, 2000, 4000, 8000}[rng.IntN(6)]
		gibPerCPU := []int64{1, 2, 4, 8}[rng.IntN(4)]
		need := Requirement{CPUMillis: cpus, MemoryBytes: min(max(int64(cpus)*gibPerCPU*gib/1000, gib/2), 64*gib), Preemptible: rng.Float64() >= 0.2}
		if rng.Float64() < 0.15 {
			need.Region = []string{"us-east", "us-west"}[rng.IntN(2)]
		}
		runs := time.Duration(math.Exp(rng.NormFloat64()*1.5+math.Log(90))) * time.Second
		runs = min(max(runs, 5*time.Second), 30*time.Minute)
		build := rng.Float64() < 0.05
		if build {
			need, runs = Requirement{CPUMillis: 4000, MemoryBytes: 8 * gib, Preemptible: true}, 3*time.Minute
		}
		fn := fmt.Sprintf("c%02d", c)
		meanGap := 30 + rng.Float64()*600
		bursty := rng.Float64() < 0.3
		for at := rng.Float64() * meanGap; at < 90*60; at += rng.ExpFloat64() * meanGap {
			count := 1
			if bursty {
				count, at = 1+rng.IntN(20), at+rng.ExpFloat64()*meanGap*5
			}
			out = append(out, simArrival{at: time.Duration(at * float64(time.Second)), need: need, count: count, runs: runs, fn: fn, build: build})
		}
	}
	return out
}

// gpuQuotes are the recorded GPU Spot quotes.
func gpuQuotes(t *testing.T) [][5]json.RawMessage {
	var g struct {
		Spot [][5]json.RawMessage `json:"spot"`
	}
	readTestJSON(t, "gpu_spot.json", &g)
	return g.Spot
}

func platformScenarios() []platformScenario {
	withGPUs := func(t *testing.T, s *sim) {
		s.prices = loadFleetRun(t, "run2").quotes(t, gpuQuotes(t))
		s.recent[ReserveMarket{GPU: "T4"}] = FleetCapacity{CPUMillis: 4000, MemoryBytes: 16 * gib, GPUs: 1}
		s.recent[ReserveMarket{GPU: "A10G"}] = FleetCapacity{CPUMillis: 8000, MemoryBytes: 32 * gib, GPUs: 1}
		s.recent[ReserveMarket{GPU: "L4"}] = FleetCapacity{CPUMillis: 4000, MemoryBytes: 16 * gib, GPUs: 1}
	}
	recorded := func(t *testing.T, run fleetRun) []simArrival { return run.arrivals(t) }
	return []platformScenario{
		{name: "bursty, run 2", window: replayWindow, seeds: 4, arrivals: recorded},
		{name: "bursty, run 1", window: replayWindow, seeds: 4, arrivals: func(t *testing.T, _ fleetRun) []simArrival {
			return loadFleetRun(t, "run1").arrivals(t)
		}},
		{name: "steady", window: 120 * time.Minute, seeds: 3, arrivals: steadyLoad(1, 90*time.Minute)},
		{name: "steady 5x", window: 120 * time.Minute, seeds: 2, arrivals: steadyLoad(5, 90*time.Minute)},
		{name: "steady 20x", window: 75 * time.Minute, seeds: 1, arrivals: steadyLoad(20, 45*time.Minute)},
		{name: "recurring bursts", window: 85 * time.Minute, seeds: 3, arrivals: recurringBursts},
		{name: "GPU mix", window: 90 * time.Minute, seeds: 3, arrivals: gpuMix, setup: withGPUs},
		{name: "many customers", window: 120 * time.Minute, seeds: 3, arrivals: manyCustomers, setup: func(t *testing.T, s *sim) {
			withGPUs(t, s)
			short := shortSpot(s.in.Spot)
			s.refuses = func(region, zoneID, instanceType string, market Market) bool {
				return market == MarketSpot && short[region+"/"+zoneID+"/"+instanceType]
			}
		}},
		{name: "connected account", window: 120 * time.Minute, seeds: 3, arrivals: steadyLoad(1, 90*time.Minute), setup: func(_ *testing.T, s *sim) {
			s.connected, s.in.OwnerPays = true, true
			s.in.Networks = map[string]Network{"us-east-2": s.in.Networks["us-east-2"]}
		}},
	}
}

// platformOutcome is one seed's measures; money is in dollars.
type platformOutcome struct {
	revenue, spend, idleSpend          float64
	fleetCPU, fleetGiB, billedCPU, gib float64
	unbilled                           map[string]*capacityUse
	waits                              map[string][]time.Duration
	lost, reclaims, reserves, resumes  int
	// connected is set on a connected account's run, which pays its own
	// hosts and bills no revenue here.
	connected bool
}

func (o platformOutcome) profit() float64 { return o.revenue - (o.spend - o.idleSpend) }

// runPlatform runs one seed of sc on the platform fleet as run 2 found it,
// and the same fleet without the work for the idle spend.
func runPlatform(t *testing.T, sc platformScenario, seed uint64) platformOutcome {
	run := loadFleetRun(t, "run2")
	sim := func() *sim {
		s := newRunSim(t, run, seed)
		s.hazard, s.quotas, s.knowQuota, s.split = reclaimHazard(t, prodHazardScale), recordedQuotas(), true, true
		if sc.setup != nil {
			sc.setup(t, s)
			s.in.Spot = s.prices(s.now)
		}
		return s
	}
	busy, idle := sim(), sim()
	r := busy.run(sc.window, sc.arrivals(t, run))
	ir := idle.run(sc.window, nil)
	if len(busy.pending) > 0 || len(r.violations) > 0 {
		t.Errorf("%s seed %d: %d containers unplaced, violations %v", sc.name, seed, len(busy.pending), r.violations)
	}
	o := platformOutcome{
		revenue: float64(r.revenueNanos) / 1e9, spend: float64(r.windowMicros-r.refundMicros) / 3600e6, idleSpend: float64(ir.windowMicros-ir.refundMicros) / 3600e6,
		fleetCPU: r.fleetCPUSec / 3600, fleetGiB: r.fleetGiBSec / 3600, billedCPU: r.billedCPUSec / 3600, gib: r.billedGiBSec / 3600,
		unbilled: r.unbilled, waits: r.fnWaits, lost: r.lost, reclaims: r.reclaims, reserves: len(r.reserveBought), resumes: r.resumes,
		connected: busy.connected,
	}
	// The buckets split exactly what the running hosts held.
	var split float64
	for _, u := range r.unbilled {
		split += u.cpu / 3600
	}
	if math.Abs(split-o.fleetCPU) > 0.001*o.fleetCPU+0.01 {
		t.Errorf("%s seed %d: buckets hold %.2f CPU-h of the fleet's %.2f", sc.name, seed, split, o.fleetCPU)
	}
	if o.billedCPU > o.fleetCPU*1.001 || o.revenue < 0 || (!busy.connected && o.revenue == 0) {
		t.Errorf("%s seed %d: billed %.1f of %.1f CPU-h, revenue $%.2f", sc.name, seed, o.billedCPU, o.fleetCPU, o.revenue)
	}
	return o
}

// platformRow is a scenario's report line: means over its seeds, the
// range in brackets where seeds differ.
func platformRow(name string, outs []platformOutcome) string {
	mean := func(f func(platformOutcome) float64) string {
		xs := make([]float64, len(outs))
		for i, o := range outs {
			xs[i] = f(o)
		}
		m := 0.0
		for _, x := range xs {
			m += x / float64(len(xs))
		}
		if lo, hi := slices.Min(xs), slices.Max(xs); hi-lo > 0.005*math.Abs(m)+0.005 {
			return fmt.Sprintf("%.2f [%.2f-%.2f]", m, lo, hi)
		}
		return fmt.Sprintf("%.2f", m)
	}
	var b strings.Builder
	money := "profit $" + mean(platformOutcome.profit)
	if outs[0].connected {
		money = "account pays $" + mean(func(o platformOutcome) float64 { return o.spend - o.idleSpend }) + " above idle"
	}
	fmt.Fprintf(&b, "%-18s %s  billed CPU %s mem %s  preempted %s reclaims %s  reserves bought %s resumed %s\n", name,
		money, mean(func(o platformOutcome) float64 { return o.billedCPU / o.fleetCPU }),
		mean(func(o platformOutcome) float64 { return o.gib / o.fleetGiB }), mean(func(o platformOutcome) float64 { return float64(o.lost) }),
		mean(func(o platformOutcome) float64 { return float64(o.reclaims) }), mean(func(o platformOutcome) float64 { return float64(o.reserves) }),
		mean(func(o platformOutcome) float64 { return float64(o.resumes) }))
	fmt.Fprintf(&b, "%18s unbilled CPU-h:", "")
	for _, bk := range buckets[1:] {
		fmt.Fprintf(&b, " %s %s", bk, mean(func(o platformOutcome) float64 { return o.unbilled[bk].cpu / 3600 }))
	}
	pooled := map[string][]time.Duration{}
	for _, o := range outs {
		for fn, w := range o.waits {
			pooled[fn] = append(pooled[fn], w...)
		}
	}
	fmt.Fprintf(&b, "\n%18s start p50/p95 s:", "")
	fns := slices.SortedFunc(maps.Keys(pooled), func(a, b string) int { return cmp.Compare(a, b) })
	for _, fn := range fns {
		fmt.Fprintf(&b, " %s %.0f/%.0f", fn, quantile(pooled[fn], 0.5).Seconds(), quantile(pooled[fn], 0.95).Seconds())
	}
	return b.String()
}

func TestFleetPlatformScenarios(t *testing.T) {
	t.Parallel()
	if testing.Short() {
		t.Skip("the platform scenarios take minutes")
	}
	for _, sc := range platformScenarios() {
		t.Run(sc.name, func(t *testing.T) {
			t.Parallel()
			outs := make([]platformOutcome, sc.seeds)
			var wg sync.WaitGroup
			for n := range outs {
				wg.Go(func() { outs[n] = runPlatform(t, sc, uint64(n)+1) })
			}
			wg.Wait()
			t.Log("\n" + platformRow(sc.name, outs))
		})
	}
}
