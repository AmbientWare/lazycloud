package compute

import (
	"slices"
	"time"

	"github.com/AmbientWare/lazycloud/internal/cpu"
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
	return FleetCapacity{CPUMillis: a.CPUMillis * cpu.Millis(n), MemoryBytes: a.MemoryBytes * int64(n), GPUs: a.GPUs * n}
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
	up := func(v int64) int64 { return (v*p + 99) / 100 }
	return FleetCapacity{CPUMillis: cpu.Millis(up(int64(a.CPUMillis))), MemoryBytes: up(a.MemoryBytes), GPUs: int(up(int64(a.GPUs)))}
}

// Empty reports whether a has nothing positive in any dimension.
func (a FleetCapacity) Empty() bool { return FleetCapacity{}.Covers(a) }

// fits reports how many copies of shape fit in a, bounded by limit.
func (a FleetCapacity) fits(shape FleetCapacity, limit int) int {
	n := int64(limit)
	for _, d := range [][2]int64{{int64(a.CPUMillis), int64(shape.CPUMillis)}, {a.MemoryBytes, shape.MemoryBytes}, {int64(a.GPUs), int64(shape.GPUs)}} {
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

// spotCPU reports the CPU Spot market, the one whose purchases follow
// its load and arrivals.
func (m ReserveMarket) spotCPU() bool { return m == ReserveMarket{Preemptible: true} }

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
	// FitLargest raises the stopped target to at least the floor beside the
	// market's largest recent shape (Policy.LargestShape), and one of the
	// machines that hold the target must fit that shape.
	FitLargest bool
}

// LargestShape sizes the reserve that fits a market's largest shape: the
// largest CPU, memory and GPUs its containers reserved within Window,
// capped at Cap. A CPU market without such containers keeps one of
// Default; a GPU market keeps none. Cap also bounds a warm slot.
type LargestShape struct {
	Window       time.Duration
	Default, Cap FleetCapacity
}

// of is the shape market m's reserve fits, given the largest its containers
// reserved; empty for none. A GPU reserve holds one card; a CPU reserve
// fits at least the default.
func (s LargestShape) of(m ReserveMarket, largest FleetCapacity) FleetCapacity {
	switch {
	case m.GPU != "" && largest.GPUs == 0:
		return FleetCapacity{}
	case m.GPU != "":
		largest.GPUs = 1
	default:
		largest = largest.Upper(s.Default)
	}
	return largest.Lower(s.Cap)
}

// BatchWindow holds purchases while containers arrive: until Quiet passes
// without an arrival, and at most Max after the first.
type BatchWindow struct {
	Quiet, Max time.Duration
}

// Policy is the fleet capacity policy, reviewed like prices.
type Policy struct {
	// MarginPercent is the share of rate-card revenue a purchase must keep
	// after its supplier cost.
	MarginPercent int64
	// Provision is how long a bought host runs, paid for, before it serves
	// or stops as a reserve.
	Provision time.Duration
	// CostHorizon is how long a purchase is priced over.
	CostHorizon time.Duration
	// MaxGrowthActions bounds the resumes and purchases of one market in
	// one pass; the rest wait for the next pass.
	MaxGrowthActions int
	Spot, OnDemand   MarketReserve
	// GPU is the on-demand reserve per GPU model; a model left out keeps
	// none.
	GPU          map[string]MarketReserve
	LargestShape LargestShape
	// BuildWindow is how long a build placed in a CPU market keeps a warm slot
	// of its shape there once it ends.
	BuildWindow time.Duration
	// LongestBuild is the most an image build runs, the images owner's
	// build timeout.
	LongestBuild time.Duration
	// ArrivalWindow is a margin over how long a Spot launch takes to serve
	// work, about a minute: the Spot market keeps warm at least what was
	// placed from arrivals within it, less their largest batch, so work
	// arriving at a steady rate finds room rather than waiting for a
	// launch, while a single burst buys no headroom it cannot use.
	ArrivalWindow time.Duration
	Batch         BatchWindow
	// IdleTimeout is how long a serving host stays idle before it leaves.
	IdleTimeout time.Duration
	// ReturnWait is how long a market waits for a host that could return to
	// the reserve, as one resumed for a burst or a build does, before it
	// buys what the reserve lacks.
	ReturnWait time.Duration
	// SpotPriceAge is how old a Spot quote may be and still price a
	// purchase.
	SpotPriceAge time.Duration
}

// DefaultPolicy is the policy the planner runs.
func DefaultPolicy() Policy {
	cpuMarket := MarketReserve{
		Warm:       HeadroomTarget{Floor: FleetCapacity{CPUMillis: 1000, MemoryBytes: 4 * gib}, LoadPercent: 25},
		Stopped:    HeadroomTarget{Floor: FleetCapacity{CPUMillis: 3000, MemoryBytes: 12 * gib}, LoadPercent: 50},
		FitLargest: true,
	}
	// Spot keeps only the floor stopped: its launches serve in about 25 s,
	// so reserves sized to load cost more than the starts they speed up.
	spotMarket := cpuMarket
	spotMarket.Stopped.LoadPercent = 0
	card := MarketReserve{Warm: HeadroomTarget{LoadPercent: 25}, Stopped: HeadroomTarget{LoadPercent: 50}, FitLargest: true}
	// What a host of 8 CPU and 32 GiB, a size that hibernates, offers; and
	// the cap, what one of 16 CPU and 64 GiB offers, with one card.
	fits := CatalogType{Topology: twoPerCore(16), MemoryBytes: 32 * gib}.Usable(0)
	limit := CatalogType{Topology: twoPerCore(32), MemoryBytes: 64 * gib, GPUCount: 1}.Usable(0)
	return Policy{
		MarginPercent:    30,
		Provision:        300 * time.Second,
		CostHorizon:      time.Hour,
		MaxGrowthActions: 16,
		Spot:             spotMarket, OnDemand: cpuMarket,
		GPU:           map[string]MarketReserve{"T4": card, "A10G": card, "L4": card},
		LargestShape:  LargestShape{Window: 7 * 24 * time.Hour, Default: fits, Cap: limit},
		BuildWindow:   time.Hour,
		LongestBuild:  time.Hour,
		ArrivalWindow: 2 * time.Minute,
		Batch:         BatchWindow{Quiet: time.Second, Max: 5 * time.Second},
		IdleTimeout:   2 * time.Minute,
		ReturnWait:    5 * time.Minute,
		SpotPriceAge:  time.Hour,
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

// slotKind is why a market keeps a warm slot.
type slotKind string

const (
	// slotFloor is part of the market's floor.
	slotFloor slotKind = "floor"
	// slotLoad is part of the share of load the market adds beyond its
	// floor; it follows the load.
	slotLoad slotKind = "load"
	// slotBuild is a recent build's shape.
	slotBuild slotKind = "build"
)

// warmSlot is room a market keeps free on a serving host.
type warmSlot struct {
	shape FleetCapacity
	kind  slotKind
}

// slots are the warm slots that hold warm, a target's headroom: its floor,
// then what warm adds beyond it. Each part splits into the fewest equal
// slots within its bound, the cap for the floor and the floor for the
// rest, so the slots hold the whole headroom and one fits a start of the
// floor's shape.
func (p Policy) slots(target HeadroomTarget, warm FleetCapacity) []warmSlot {
	rest := warm.Minus(target.Floor).Clamp()
	bound := p.LargestShape.Cap
	if !target.Floor.Empty() {
		bound = target.Floor.Lower(bound)
	}
	var out []warmSlot
	for _, shape := range split(target.Floor, p.LargestShape.Cap) {
		out = append(out, warmSlot{shape: shape, kind: slotFloor})
	}
	for _, shape := range split(rest, bound) {
		out = append(out, warmSlot{shape: shape, kind: slotLoad})
	}
	return out
}

// split divides c into the fewest equal parts, rounded up, that each fit
// within bound in every dimension bound has; none when c is empty.
func split(c, bound FleetCapacity) []FleetCapacity {
	if c.Empty() {
		return nil
	}
	n := int64(1)
	for _, d := range [][2]int64{{int64(c.CPUMillis), int64(bound.CPUMillis)}, {c.MemoryBytes, bound.MemoryBytes}, {int64(c.GPUs), int64(bound.GPUs)}} {
		if d[1] > 0 {
			n = max(n, (d[0]+d[1]-1)/d[1])
		}
	}
	up := func(v int64) int64 { return (v + n - 1) / n }
	part := FleetCapacity{CPUMillis: cpu.Millis(up(int64(c.CPUMillis))), MemoryBytes: up(c.MemoryBytes), GPUs: int(up(int64(c.GPUs)))}
	return slices.Repeat([]FleetCapacity{part}, int(n))
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
