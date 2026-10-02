package compute

import (
	"slices"
	"time"
)

// The fleet policy is how much spare capacity the platform fleet keeps per
// purchase market. Headroom is CPU, memory and GPUs rather than machines,
// because a request needs room of a size, not a machine count. Every figure
// is usable capacity, the numbers hosts advertise.

// Plus is a + b in every dimension.
func (a FleetCapacity) Plus(b FleetCapacity) FleetCapacity {
	return FleetCapacity{CPUMillis: a.CPUMillis + b.CPUMillis, MemoryBytes: a.MemoryBytes + b.MemoryBytes, GPUs: a.GPUs + b.GPUs}
}

// Minus is a - b in every dimension; it may go negative.
func (a FleetCapacity) Minus(b FleetCapacity) FleetCapacity {
	return FleetCapacity{CPUMillis: a.CPUMillis - b.CPUMillis, MemoryBytes: a.MemoryBytes - b.MemoryBytes, GPUs: a.GPUs - b.GPUs}
}

// Times is n copies of a.
func (a FleetCapacity) Times(n int) FleetCapacity {
	return FleetCapacity{CPUMillis: a.CPUMillis * int64(n), MemoryBytes: a.MemoryBytes * int64(n), GPUs: a.GPUs * n}
}

// Covers reports whether a is at least b in every dimension.
func (a FleetCapacity) Covers(b FleetCapacity) bool {
	return a.CPUMillis >= b.CPUMillis && a.MemoryBytes >= b.MemoryBytes && a.GPUs >= b.GPUs
}

// Upper is the larger of a and b in each dimension.
func (a FleetCapacity) Upper(b FleetCapacity) FleetCapacity {
	return FleetCapacity{CPUMillis: max(a.CPUMillis, b.CPUMillis), MemoryBytes: max(a.MemoryBytes, b.MemoryBytes), GPUs: max(a.GPUs, b.GPUs)}
}

// Lower is the smaller of a and b in each dimension.
func (a FleetCapacity) Lower(b FleetCapacity) FleetCapacity {
	return FleetCapacity{CPUMillis: min(a.CPUMillis, b.CPUMillis), MemoryBytes: min(a.MemoryBytes, b.MemoryBytes), GPUs: min(a.GPUs, b.GPUs)}
}

// Clamp raises negative dimensions to zero.
func (a FleetCapacity) Clamp() FleetCapacity { return a.Upper(FleetCapacity{}) }

// Percent is p percent of a, each dimension rounded up.
func (a FleetCapacity) Percent(p int64) FleetCapacity {
	up := func(v int64) int64 {
		if v*p > 0 {
			return (v*p + 99) / 100
		}
		return v * p / 100
	}
	return FleetCapacity{CPUMillis: up(a.CPUMillis), MemoryBytes: up(a.MemoryBytes), GPUs: int(up(int64(a.GPUs)))}
}

// Empty reports whether a has nothing positive in any dimension.
func (a FleetCapacity) Empty() bool { return FleetCapacity{}.Covers(a) }

// fits reports how many copies of shape fit in a, bounded by limit.
func (a FleetCapacity) fits(shape FleetCapacity, limit int) int {
	n := int64(limit)
	for _, d := range [][2]int64{{a.CPUMillis, shape.CPUMillis}, {a.MemoryBytes, shape.MemoryBytes}, {int64(a.GPUs), int64(shape.GPUs)}} {
		if d[1] > 0 {
			n = min(n, max(d[0], 0)/d[1])
		}
	}
	return int(n)
}

func totalOf[T any](items []T, f func(T) FleetCapacity) FleetCapacity {
	var total FleetCapacity
	for _, item := range items {
		total = total.Plus(f(item))
	}
	return total
}

// ReserveMarket is a purchase market the fleet keeps headroom in: Spot or
// on-demand CPU capacity, or one GPU model. GPU headroom is kept on-demand;
// Spot GPU work keeps none.
type ReserveMarket struct {
	Preemptible bool
	GPU         string
}

func (m ReserveMarket) String() string {
	market, gpu := "on_demand", "cpu"
	if m.Preemptible {
		market = "spot"
	}
	if m.GPU != "" {
		gpu = m.GPU
	}
	return market + ":" + gpu
}

// buyMarket is how a market's own purchases are bought.
func (m ReserveMarket) buyMarket() Market {
	if m.Preemptible {
		return MarketSpot
	}
	return MarketOnDemand
}

// reserveMarketOf is the market a host of this purchase market and GPU
// model serves.
func reserveMarketOf(market Market, gpu string) ReserveMarket {
	return ReserveMarket{Preemptible: market == MarketSpot, GPU: gpu}
}

// HeadroomTarget is a minimum spare capacity and the share of current load
// kept free, whichever is larger.
type HeadroomTarget struct {
	Floor       FleetCapacity
	LoadPercent int64
}

// Of is the target at load.
func (t HeadroomTarget) Of(load FleetCapacity) FleetCapacity {
	return t.Floor.Upper(load.Percent(t.LoadPercent))
}

// MarketReserve is the running headroom a market keeps warm and the
// headroom it keeps as stopped machines.
type MarketReserve struct {
	Warm    HeadroomTarget
	Stopped HeadroomTarget
}

// Policy is the fleet capacity policy, reviewed like prices.
type Policy struct {
	// MarginPercent is the share of rate-card revenue a purchase must keep
	// after its supplier cost.
	MarginPercent int64
	// Resume, StoppedBoot and Provision are the activation defaults when
	// too few activations were observed.
	Resume, StoppedBoot, Provision time.Duration
	// ActivationSamples is how many ready activations make a p95 usable.
	ActivationSamples int
	// PlanInterval is the planning cadence; EarlyPlanInterval the faster
	// one while running headroom has been short for Pressure.
	PlanInterval, EarlyPlanInterval, Pressure time.Duration
	// CostHorizon is how long a purchase is priced over.
	CostHorizon time.Duration
	// MaxGrowthActions bounds the resumes and purchases of one market in
	// one pass; the rest wait for the next pass.
	MaxGrowthActions int
	Spot, OnDemand   MarketReserve
	// GPU is the on-demand reserve per GPU model; a model left out keeps
	// none.
	GPU map[string]MarketReserve
	// A serving host at or under ConsolidationPercent of every dimension
	// for ConsolidationLight is lightly used. Consolidation waits
	// ConsolidationCooldown between moves and gives one up after
	// ConsolidationDeadline.
	ConsolidationPercent                                             int64
	ConsolidationLight, ConsolidationCooldown, ConsolidationDeadline time.Duration
	// IdleTimeout is the fleet's configured idle wait; an idle host leaves
	// after the later of it and ConsolidationLight.
	IdleTimeout time.Duration
	// BillingMinimum is what EC2 bills a started instance at least.
	BillingMinimum time.Duration
	// SpotPriceAge is how old a Spot quote may be and still price a
	// purchase.
	SpotPriceAge time.Duration
	// RegionFailures refusals from distinct offers of one region within
	// RegionFailureWindow rank that region after the others.
	RegionFailures      int
	RegionFailureWindow time.Duration
	// ShortWindow and History are the forecast's arrival windows;
	// RequestShapes bounds the shapes a forecast keeps.
	ShortWindow, History time.Duration
	RequestShapes        int
}

// DefaultPolicy is the policy the planner runs.
func DefaultPolicy() Policy {
	// The CPU floors keep about $231/month of warm spares and stopped
	// reserves at zero load.
	cpu := MarketReserve{
		Warm:    HeadroomTarget{Floor: FleetCapacity{CPUMillis: 2000, MemoryBytes: 4 * gib}, LoadPercent: 25},
		Stopped: HeadroomTarget{Floor: FleetCapacity{CPUMillis: 6000, MemoryBytes: 12 * gib}, LoadPercent: 50},
	}
	card := MarketReserve{Warm: HeadroomTarget{LoadPercent: 25}, Stopped: HeadroomTarget{LoadPercent: 50}}
	return Policy{
		MarginPercent: 30,
		Resume:        30 * time.Second, StoppedBoot: 120 * time.Second, Provision: 300 * time.Second,
		ActivationSamples: 20,
		PlanInterval:      60 * time.Second, EarlyPlanInterval: 20 * time.Second, Pressure: 5 * time.Second,
		CostHorizon:      time.Hour,
		MaxGrowthActions: 16,
		Spot:             cpu, OnDemand: cpu,
		GPU:                  map[string]MarketReserve{"T4": card, "A10G": card, "L4": card},
		ConsolidationPercent: 30,
		ConsolidationLight:   600 * time.Second, ConsolidationCooldown: 900 * time.Second, ConsolidationDeadline: time.Hour,
		IdleTimeout:    5 * time.Minute,
		BillingMinimum: 60 * time.Second,
		SpotPriceAge:   time.Hour,
		RegionFailures: 2, RegionFailureWindow: 30 * time.Minute,
		ShortWindow: 60 * time.Second, History: 600 * time.Second,
		RequestShapes: 32,
	}
}

// Reserve is the headroom market m keeps.
func (p Policy) Reserve(m ReserveMarket) MarketReserve {
	switch {
	case m.GPU != "" && m.Preemptible:
		return MarketReserve{}
	case m.GPU != "":
		return p.GPU[m.GPU]
	case m.Preemptible:
		return p.Spot
	}
	return p.OnDemand
}

// Markets are the markets the policy keeps headroom in, in a fixed order.
func (p Policy) Markets() []ReserveMarket {
	markets := []ReserveMarket{{Preemptible: true}, {}}
	cards := make([]string, 0, len(p.GPU))
	for card := range p.GPU {
		cards = append(cards, card)
	}
	slices.Sort(cards)
	for _, card := range cards {
		markets = append(markets, ReserveMarket{GPU: card})
	}
	return markets
}

// WarmHorizon is the default reach of the warm forecast: a resume and the
// pass that orders it.
func (p Policy) WarmHorizon() time.Duration { return p.Resume + p.PlanInterval }

// TotalHorizon is the default reach of the total forecast: a provision and
// the pass that orders it.
func (p Policy) TotalHorizon() time.Duration { return p.Provision + p.PlanInterval }

// MarketTargets are what one market keeps: running free room, stopped
// reserve, and the part of the reserve that hibernates.
type MarketTargets struct {
	Warm, Stopped, Hibernation FleetCapacity
}

// TargetInputs are what a market's targets depend on.
type TargetInputs struct {
	Load     FleetCapacity
	Forecast *MarketForecast
	// RunningLoads are the loads of the market's running hosts.
	RunningLoads []FleetCapacity
	// HibernationShapes are the usable shapes of hibernation-capable types
	// the market holds or can buy as reserves.
	HibernationShapes []FleetCapacity
	// Shapes are the market's recent request shapes.
	Shapes []FleetCapacity
}

// TargetsFor sets a market's targets. Warm is the floor or a share of load,
// raised to the forecast's warm; the stopped reserve is the rest of the
// total target. A Spot CPU reserve must take any running host's work, and a
// CPU market hibernates its whole reserve when every recent shape fits a
// hibernation-capable shape.
func TargetsFor(p Policy, m ReserveMarket, in TargetInputs) MarketTargets {
	reserve := p.Reserve(m)
	var forecastWarm, forecastTotal FleetCapacity
	if in.Forecast != nil {
		forecastWarm, forecastTotal = in.Forecast.Warm, in.Forecast.Total
	}
	warm := reserve.Warm.Of(in.Load).Upper(forecastWarm)
	total := reserve.Stopped.Of(in.Load).Plus(warm).Upper(forecastTotal)
	stopped := total.Minus(warm).Clamp()
	if m.Preemptible && m.GPU == "" {
		// An interrupted Spot host's work must fit the reserve that
		// replaces it.
		for _, load := range in.RunningLoads {
			stopped = stopped.Upper(load)
		}
	}
	var hibernation FleetCapacity
	if m.GPU == "" && len(in.HibernationShapes) > 0 && everyShapeFits(in.Shapes, in.HibernationShapes) {
		hibernation = stopped
	}
	return MarketTargets{Warm: warm, Stopped: stopped.Upper(hibernation), Hibernation: hibernation}
}

// everyShapeFits reports whether each shape fits one of hosts.
func everyShapeFits(shapes, hosts []FleetCapacity) bool {
	for _, shape := range shapes {
		if !slices.ContainsFunc(hosts, func(h FleetCapacity) bool { return h.Covers(shape) }) {
			return false
		}
	}
	return true
}
