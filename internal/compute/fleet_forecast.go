package compute

import (
	"cmp"
	"math"
	"slices"
	"time"

	"github.com/google/uuid"
)

// Arrival is Count containers of one shape that arrived, or are expected,
// at At. A zero Duration means how long they run is unknown.
type Arrival struct {
	At       time.Time
	Shape    FleetCapacity
	Count    int
	Duration time.Duration
}

// MarketForecast is the capacity a market expects to need beyond what it
// runs now: Warm within the warm horizon, Total within the total horizon.
type MarketForecast struct {
	Warm, Total FleetCapacity
	// Largest is the largest request observed or scheduled.
	Largest FleetCapacity
	// Shapes are the recent and scheduled request shapes, bounded.
	Shapes  []FleetCapacity
	Samples int
	// ShortArrivals and LongArrivals total the arrivals in the short and
	// long windows; Burst is the largest observed request.
	ShortArrivals, LongArrivals, Burst FleetCapacity
	ScheduledWarm, ScheduledTotal      FleetCapacity
	WarmHorizon, TotalHorizon          time.Duration
	// Timing is the activation evidence that set the warm horizon.
	Timing *ActivationTiming
}

// ForecastDemand forecasts one market over two cumulative horizons: the
// larger of the occupancy over the short and long windows, plus pending
// demand and the largest request, plus the peak of scheduled arrivals
// within each horizon. Pending capacity counts once in each.
func ForecastDemand(p Policy, now time.Time, arrivals, scheduled []Arrival, pending FleetCapacity, pendingShapes []FleetCapacity, warm, total time.Duration) MarketForecast {
	f := MarketForecast{WarmHorizon: warm, TotalHorizon: total}
	var observed []Arrival
	shapes := slices.Clone(pendingShapes)
	for _, a := range arrivals {
		if !a.At.After(now.Add(-p.History)) || a.At.After(now) || a.Count < 1 {
			continue
		}
		f.LongArrivals = f.LongArrivals.Plus(a.Shape.Times(a.Count))
		if a.At.After(now.Add(-p.ShortWindow)) {
			f.ShortArrivals = f.ShortArrivals.Plus(a.Shape.Times(a.Count))
		}
		f.Burst = f.Burst.Upper(a.Shape)
		f.Samples += a.Count
		shapes = append(shapes, a.Shape)
		observed = append(observed, a)
	}
	f.Largest = f.Burst
	type event struct {
		at    time.Duration
		delta FleetCapacity
	}
	var events []event
	for _, a := range scheduled {
		until := a.At.Sub(now)
		if until <= 0 || until > total || a.Count < 1 {
			continue
		}
		load := a.Shape.Times(a.Count)
		events = append(events, event{until, load})
		if a.Duration > 0 {
			events = append(events, event{until + a.Duration, FleetCapacity{}.Minus(load)})
		}
		f.Largest = f.Largest.Upper(a.Shape)
		shapes = append(shapes, a.Shape)
	}
	slices.SortStableFunc(events, func(a, b event) int {
		return cmp.Or(cmp.Compare(a.at, b.at), cmp.Compare(a.delta.CPUMillis, b.delta.CPUMillis),
			cmp.Compare(a.delta.MemoryBytes, b.delta.MemoryBytes), cmp.Compare(a.delta.GPUs, b.delta.GPUs))
	})
	var live FleetCapacity
	for _, e := range events {
		live = live.Plus(e.delta)
		if e.at <= total {
			f.ScheduledTotal = f.ScheduledTotal.Upper(live)
		}
		if e.at <= warm {
			f.ScheduledWarm = f.ScheduledWarm.Upper(live)
		}
	}
	horizon := func(h time.Duration) FleetCapacity {
		occupancy := func(window time.Duration) FleetCapacity {
			var cpu, memory, gpus float64
			for _, a := range observed {
				if !a.At.After(now.Add(-window)) {
					continue
				}
				occupied := h
				if a.Duration > 0 {
					occupied = min(h, a.Duration)
				}
				factor := occupied.Seconds() * float64(a.Count) / window.Seconds()
				cpu += float64(a.Shape.CPUMillis) * factor
				memory += float64(a.Shape.MemoryBytes) * factor
				gpus += float64(a.Shape.GPUs) * factor
			}
			return FleetCapacity{CPUMillis: int64(math.Ceil(cpu)), MemoryBytes: int64(math.Ceil(memory)), GPUs: int(math.Ceil(gpus))}
		}
		return pending.Plus(occupancy(p.ShortWindow).Upper(occupancy(p.History))).Plus(f.Burst)
	}
	f.Warm = horizon(warm).Plus(f.ScheduledWarm)
	f.Total = horizon(total).Plus(f.ScheduledTotal)
	f.Shapes = mergeShapes(shapes, p.RequestShapes)
	return f
}

// ActivationStat aggregates recent activations of one kind on one hardware.
type ActivationStat struct {
	Kind         ActivationKind
	InstanceType string
	Region       string
	GPU          string
	// Outcome is set for resumes; nil otherwise.
	Outcome *ResumeOutcome
	Ready   int
	Failed  int
	// P95 is the 95th percentile time to ready; nil without a ready one.
	P95 *time.Duration
}

// ActivationTiming is the evidence behind an activation estimate.
type ActivationTiming struct {
	Kind      ActivationKind
	Ready     int
	Failed    int
	ColdBoots int
	// Fallback is set when the policy default, not observation, decided.
	Fallback bool
}

// ActivationEstimate is how long an activation of kind takes, plus the pass
// that orders it. It takes the observed p95 when every matching row has at
// least ActivationSamples ready activations, else the fallback; any failure
// raises it to failureFallback and any cold boot to coldFallback.
func ActivationEstimate(p Policy, stats []ActivationStat, kind ActivationKind, fallback, failureFallback, coldFallback time.Duration) (time.Duration, ActivationTiming) {
	timing := ActivationTiming{Kind: kind}
	var observed []time.Duration
	matched := false
	for _, s := range stats {
		if s.Kind != kind {
			continue
		}
		matched = true
		timing.Ready += s.Ready
		timing.Failed += s.Failed
		if s.Outcome != nil && *s.Outcome == ResumeColdBoot {
			timing.ColdBoots += s.Ready
		}
		if s.Ready < p.ActivationSamples || s.P95 == nil {
			timing.Fallback = true
		}
		if s.P95 != nil {
			observed = append(observed, *s.P95)
		}
	}
	timing.Fallback = timing.Fallback || !matched || timing.Failed > 0
	baseline := fallback
	if timing.Failed > 0 {
		baseline = max(baseline, failureFallback)
	}
	if timing.ColdBoots > 0 {
		baseline = max(baseline, coldFallback)
	}
	seconds := baseline
	if len(observed) > 0 {
		seconds = slices.Max(observed)
	}
	if timing.Fallback || timing.ColdBoots > 0 {
		seconds = max(seconds, baseline)
		timing.Fallback = true
	}
	return seconds + p.PlanInterval, timing
}

// ForecastInput is one market's demand and the reserves that could meet it.
type ForecastInput struct {
	Market ReserveMarket
	// Region is a product region the demand is pinned to; empty is any.
	Region        string
	Arrivals      []Arrival
	Scheduled     []Arrival
	Pending       FleetCapacity
	PendingShapes []FleetCapacity
	Stats         []ActivationStat
	Hosts         []FleetHost
}

// ForecastMarket forecasts a market with horizons from its activations. The
// total horizon is a provision. The warm horizon is the fastest reserve
// class whose ready reserves cover the forecast and fit every shape: saved
// hibernations, then any hibernation, then any ready reserve; the slowest
// host of that class sets it. Without such a class, a provision does.
func ForecastMarket(p Policy, now time.Time, in ForecastInput) MarketForecast {
	var provisionStats []ActivationStat
	for _, s := range in.Stats {
		if s.GPU == in.Market.GPU && (in.Region == "" || ProductRegion(s.Region) == in.Region) {
			provisionStats = append(provisionStats, s)
		}
	}
	provision, provisionTiming := ActivationEstimate(p, provisionStats, ActivationProvision, p.Provision, p.Provision, p.Provision)
	full := ForecastDemand(p, now, in.Arrivals, in.Scheduled, in.Pending, in.PendingShapes, provision, provision)
	var ready []FleetHost
	for _, h := range in.Hosts {
		if h.resumable() && h.Current && !h.Protected && h.market() == in.Market && (in.Region == "" || ProductRegion(h.Region) == in.Region) {
			ready = append(ready, h)
		}
	}
	shapes := full.Shapes
	covered := func(states ...FleetState) []FleetHost {
		var hosts []FleetHost
		for _, h := range ready {
			if slices.Contains(states, h.State) {
				hosts = append(hosts, h)
			}
		}
		usable := make([]FleetCapacity, len(hosts))
		for i, h := range hosts {
			usable[i] = h.Usable
		}
		if len(hosts) == 0 || !totalOf(usable, func(c FleetCapacity) FleetCapacity { return c }).Covers(full.Warm) || !everyShapeFits(shapes, usable) {
			return nil
		}
		return hosts
	}
	selected := covered(FleetImageSaved)
	if selected == nil {
		selected = covered(FleetImageSaved, FleetHibernateUnverified)
	}
	if selected == nil {
		selected = covered(FleetImageSaved, FleetHibernateUnverified, FleetStopped)
	}
	if selected == nil {
		full.Timing = &provisionTiming
		return full
	}
	var warm time.Duration
	var timing ActivationTiming
	for _, h := range selected {
		var matching []ActivationStat
		for _, s := range in.Stats {
			if s.GPU == in.Market.GPU && s.InstanceType == h.InstanceType && s.Region == h.Region {
				matching = append(matching, s)
			}
		}
		// A saved hibernation resumes; an unverified one may boot cold; a
		// plain stop boots.
		kind, fallback := ActivationResume, p.StoppedBoot
		if h.State == FleetImageSaved {
			fallback = p.Resume
		}
		if h.State == FleetStopped {
			kind = ActivationBoot
		}
		seconds, t := ActivationEstimate(p, matching, kind, fallback, p.Provision, p.StoppedBoot)
		if seconds > warm {
			warm, timing = seconds, t
		}
	}
	f := ForecastDemand(p, now, in.Arrivals, in.Scheduled, in.Pending, in.PendingShapes, warm, max(warm, provision))
	f.Timing = &timing
	return f
}

// ScheduledWorkload is one scheduled function's runs within the horizon
// and how its containers serve them.
type ScheduledWorkload struct {
	ID    uuid.UUID
	Shape FleetCapacity
	// Concurrency is how many runs one container serves at once.
	Concurrency int
	// MaxContainers caps the function's containers; below one means one.
	MaxContainers int
	// Existing containers serve runs only when AlwaysWarm keeps them.
	Existing   int
	AlwaysWarm bool
	// KeepWarm is how long an idle container stays after its last run.
	KeepWarm time.Duration
	// Duration is how long a run takes; zero means the whole horizon.
	Duration time.Duration
	Runs     []ScheduledRun
}

// ScheduledRun is Count invocations due at At.
type ScheduledRun struct {
	At    time.Time
	Count int
}

// ScheduledArrivals expands scheduled runs into the containers they start:
// runs share a container up to its concurrency and while it stays warm,
// within the function's container ceiling, and kept-warm containers serve
// runs without new capacity. Each arrival lasts as long as its container.
func ScheduledArrivals(workloads []ScheduledWorkload, now, until time.Time) []Arrival {
	type container struct {
		created  *time.Time
		expires  time.Time
		finishes []time.Time
	}
	var out []Arrival
	emit := func(w ScheduledWorkload, c *container) {
		if c.created != nil {
			out = append(out, Arrival{At: *c.created, Shape: w.Shape, Count: 1, Duration: c.expires.Sub(*c.created)})
		}
	}
	for _, w := range workloads {
		runs := slices.Clone(w.Runs)
		slices.SortStableFunc(runs, func(a, b ScheduledRun) int { return a.At.Compare(b.At) })
		retained := until.Add(time.Second)
		ceiling := max(w.MaxContainers, 1)
		concurrency := max(w.Concurrency, 1)
		var containers []*container
		if w.AlwaysWarm {
			for range w.Existing {
				containers = append(containers, &container{expires: retained})
			}
		}
		for _, run := range runs {
			at := run.At
			kept := containers[:0]
			for _, c := range containers {
				c.finishes = slices.DeleteFunc(c.finishes, func(end time.Time) bool { return !end.After(at) })
				if !c.expires.After(at) && len(c.finishes) == 0 {
					emit(w, c)
					continue
				}
				kept = append(kept, c)
			}
			containers = kept
			for range run.Count {
				var available *container
				for _, c := range containers {
					if len(c.finishes) < concurrency {
						available = c
						break
					}
				}
				startsAt := at
				if available == nil {
					if len(containers) >= ceiling {
						available = slices.MinFunc(containers, func(a, b *container) int {
							return earliest(a.finishes).Compare(earliest(b.finishes))
						})
						first := earliest(available.finishes)
						startsAt = first
						i := slices.IndexFunc(available.finishes, first.Equal)
						available.finishes = slices.Delete(available.finishes, i, i+1)
					} else {
						created := at
						available = &container{created: &created, expires: until}
						containers = append(containers, available)
					}
				}
				duration := w.Duration
				if duration <= 0 {
					duration = max(until.Sub(now), time.Second)
				}
				available.finishes = append(available.finishes, startsAt.Add(duration))
				if w.AlwaysWarm {
					available.expires = retained
				} else {
					available.expires = slices.MaxFunc(available.finishes, time.Time.Compare).Add(w.KeepWarm)
				}
			}
		}
		for _, c := range containers {
			emit(w, c)
		}
	}
	return out
}

func earliest(times []time.Time) time.Time { return slices.MinFunc(times, time.Time.Compare) }
