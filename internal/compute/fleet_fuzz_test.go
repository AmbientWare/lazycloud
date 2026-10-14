package compute

import (
	"maps"
	"math/rand/v2"
	"slices"
	"testing"
	"time"
)

// FuzzFleetInvariants runs the simulation over a load, refusals and deploys
// drawn from the seed and fails on any invariant a pass breaks, as the
// simulation watches them (I1, I2, I3 and I5), on any container left
// unplaced that did not time out, and on spend that grows faster than the
// work: beyond what the same fleet spends with no load and, three times
// over for its reserves and their replacements after an agent change, a
// host of its own for each container from launch to the end of its
// market's window. CI runs the seeds below and the corpus in
// testdata/fuzz.
func FuzzFleetInvariants(f *testing.F) {
	for seed := range uint64(6) {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, seed uint64) {
		// Work arrives for d and the run lasts until the longest has ended.
		const d, run = 20 * time.Minute, 32 * time.Minute
		rng := rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15))
		arrivals := fuzzArrivals(rng, d)
		configure := fuzzFleet(rng)
		quiet, s := newProdSim(t, false), newProdSim(t, false)
		configure(quiet)
		configure(s)
		idle := quiet.run(run, nil)
		r := s.run(run, arrivals)
		for _, b := range slices.Concat(r.breaches, r.violations) {
			t.Error(b)
		}
		want := 0
		for _, a := range arrivals {
			want += a.count
		}
		if left := want - len(r.waits) - r.failed; left != len(s.pending) {
			t.Errorf("%d of %d containers placed, %d timed out, %d pending", len(r.waits), want, r.failed, len(s.pending))
		}
		if spend, bound := r.spendMicros, idle.spendMicros+3*dedicatedMicros(t, s, arrivals); spend > bound {
			t.Errorf("spent $%.2f, bound $%.2f", float64(spend)/3600e6, float64(bound)/3600e6)
		}
	})
}

// dedicatedMicros is what serving each arrival on a host of its own costs:
// the cheapest offer that hosts it, from its launch through its run and its
// market's window, in micros a second.
func dedicatedMicros(t *testing.T, s *sim, arrivals []simArrival) int64 {
	var total int64
	for _, a := range arrivals {
		offers := RankOffers(s.p, a.need, false, s.in)
		if len(offers) == 0 {
			t.Fatalf("no offer hosts %+v", a.need)
		}
		launch, m := s.p.Starts.Launch, ReserveMarket{Preemptible: a.need.Preemptible}
		if a.need.GPUsNeeded() > 0 {
			launch, m.GPU = s.p.Starts.GPULaunch, offers[0].Type.GPU
		}
		total += int64(a.count) * offers[0].HourlyMicros * int64((launch+a.runs+s.p.Window(m))/time.Second)
	}
	return total
}

// fuzzFleet draws what EC2 refuses, the Spot quota and a deploy, with or
// without an agent change, and applies them to a prod fleet of at most 60
// hosts.
func fuzzFleet(rng *rand.Rand) func(*sim) {
	refusal, pick, model, quota := rng.IntN(4), rng.Uint64(), []string{"T4", "L4", "A10G"}[rng.IntN(3)], rng.Uint64()
	var deploys []simDeploy
	if n := rng.IntN(3); n > 0 {
		deploys = []simDeploy{{at: time.Duration(rng.Int64N(int64(20 * time.Minute))), agent: n == 2}}
	}
	return func(s *sim) {
		s.maxHosts, s.deploys = 60, slices.Clone(deploys)
		switch refusal {
		case 1:
			pool := s.in.Spot[pick%uint64(len(s.in.Spot))]
			s.refuses = func(region, zoneID, instanceType string, market Market) bool {
				return market == MarketSpot && region == pool.Region && zoneID == pool.ZoneID && instanceType == pool.InstanceType
			}
		case 2:
			gpu := map[string]string{}
			for _, typ := range s.in.Catalog {
				gpu[typ.Name] = typ.GPU
			}
			s.refuses = func(_, _, instanceType string, market Market) bool {
				return market == MarketSpot && gpu[instanceType] == model
			}
		case 3:
			s.knowQuota, s.quotas = true, map[QuotaKey]int64{}
			for n, region := range slices.Sorted(maps.Keys(s.in.Networks)) {
				s.quotas[QuotaKey{Region: region, Class: QuotaStandard, Market: MarketSpot}] = int64(32 + 32*((quota>>(2*n))%4))
			}
		}
	}
}

// fuzzArrivals draws one to three arrival processes over d, each of a CPU
// or GPU shape, on Spot or not: Poisson at a rate that holds 2 to 60 CPUs
// of load, on and off with periods of 5 s to 10 min, or bursts of 1 to 50.
func fuzzArrivals(rng *rand.Rand, d time.Duration) []simArrival {
	shapes := []Requirement{cpuNeed(250, 1), cpuNeed(1000, 2), cpuNeed(2000, 8), cpuNeed(4000, 16), cpuNeed(8000, 32),
		{GPUs: []string{"T4"}, GPUCount: 1, CPUMillis: 2000, MemoryBytes: 8 * gib}, l4Job,
		{GPUs: []string{"A10G"}, GPUCount: 1, CPUMillis: 1000, MemoryBytes: 20 * gib}}
	var out []simArrival
	for range 1 + rng.IntN(3) {
		need := shapes[rng.IntN(len(shapes))]
		need.Preemptible = rng.IntN(3) > 0
		gpu := need.GPUsNeeded() > 0
		longest := 2 * time.Minute
		if gpu {
			longest = 10 * time.Minute
		}
		add := func(at time.Duration, count int) {
			if at < d {
				out = append(out, simArrival{at: at, need: need, count: count, runs: time.Second + time.Duration(rng.Int64N(int64(longest)))})
			}
		}
		// rate is the arrivals a second that hold load CPUs on average.
		rate := func(load float64) float64 { return load / (float64(need.CPUMillis) / 1000 * (longest / 2).Seconds()) }
		switch kind := rng.IntN(3); {
		case kind == 0 && !gpu:
			r := rate(2 + 58*rng.Float64())
			for at := time.Duration(0); at < d; at += time.Duration(rng.ExpFloat64() / r * float64(time.Second)) {
				add(at, 1)
			}
		case kind == 1:
			period := 5*time.Second + time.Duration(rng.Int64N(int64(10*time.Minute)))
			gap := max(time.Duration(float64(time.Second)/rate(4+36*rng.Float64())), period/50)
			for on := time.Duration(0); on < d; on += 2 * period {
				for at := on; at < on+period; at += gap {
					add(at, 1)
				}
			}
		default:
			for range 1 + rng.IntN(4) {
				add(time.Duration(rng.Int64N(int64(d))), 1+rng.IntN(50))
			}
		}
	}
	slices.SortStableFunc(out, func(a, b simArrival) int { return int(a.at - b.at) })
	return out
}
