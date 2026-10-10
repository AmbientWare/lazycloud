package compute

import (
	"maps"
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

// HeadroomTarget is spare capacity kept as safety stock: what arrived at a
// steady rate within Lead, about how long new capacity of the layer takes
// to serve, and the largest batch within Memory, so a market that saw a
// burst keeps room for the next one; at least the floor.
type HeadroomTarget struct {
	Floor        FleetCapacity
	Lead, Memory time.Duration
}

// Of is the target arrivals set at now.
func (t HeadroomTarget) Of(arrivals []Arrival, now time.Time, batch BatchWindow) FleetCapacity {
	d := DemandOf(arrivals, now, t.Lead, t.Memory, batch)
	return t.Floor.Upper(d.Steady.Plus(d.Burst))
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
	// Batch is how purchases wait for arrivals, and how headroom groups
	// them into batches.
	Batch BatchWindow
	// IdleTimeout is how long a serving host stays idle before it leaves.
	IdleTimeout time.Duration
	// ReturnWait is how long a market waits for a host that could return to
	// the reserve, as one resumed for a burst or a build does, before it
	// buys what the reserve lacks.
	ReturnWait time.Duration
	// SpotPriceAge is how old a Spot quote may be and still price a
	// purchase.
	SpotPriceAge time.Duration
	// FloorHold is how long a warm floor slot that new work took from a host
	// able to hold it waits for that work to end before it buys a host.
	FloorHold time.Duration
}

// DefaultPolicy is the policy the planner runs.
func DefaultPolicy() Policy {
	// Warm room serves arrivals until a reserve resumes or a launch serves,
	// tens of seconds; reserves serve until a purchase does, minutes. Each
	// keeps room for the largest recent burst.
	warm := func(floor FleetCapacity) HeadroomTarget {
		return HeadroomTarget{Floor: floor, Lead: 2 * time.Minute, Memory: 30 * time.Minute}
	}
	stopped := func(floor FleetCapacity) HeadroomTarget {
		return HeadroomTarget{Floor: floor, Lead: 5 * time.Minute, Memory: time.Hour}
	}
	cpuFloor, reserveFloor := FleetCapacity{CPUMillis: 1000, MemoryBytes: 4 * gib}, FleetCapacity{CPUMillis: 3000, MemoryBytes: 12 * gib}
	onDemand := MarketReserve{Warm: warm(cpuFloor), Stopped: stopped(reserveFloor), FitLargest: true}
	// Spot launches serve in about 25 s, so Spot keeps only the floor
	// stopped.
	spot := MarketReserve{Warm: warm(cpuFloor), Stopped: HeadroomTarget{Floor: reserveFloor}, FitLargest: true}
	card := MarketReserve{Warm: warm(FleetCapacity{}), Stopped: stopped(FleetCapacity{}), FitLargest: true}
	// What a host of 8 CPU and 32 GiB, a size that hibernates, offers; and
	// the cap, what one of 16 CPU and 64 GiB offers, with one card.
	fits := CatalogType{Topology: twoPerCore(16), MemoryBytes: 32 * gib}.Usable(0)
	limit := CatalogType{Topology: twoPerCore(32), MemoryBytes: 64 * gib, GPUCount: 1}.Usable(0)
	return Policy{
		MarginPercent:    30,
		Provision:        300 * time.Second,
		CostHorizon:      time.Hour,
		MaxGrowthActions: 16,
		Spot:             spot, OnDemand: onDemand,
		GPU:          map[string]MarketReserve{"T4": card, "A10G": card, "L4": card},
		LargestShape: LargestShape{Window: 7 * 24 * time.Hour, Default: fits, Cap: limit},
		BuildWindow:  time.Hour,
		LongestBuild: time.Hour,
		Batch:        BatchWindow{Quiet: time.Second, Max: 5 * time.Second},
		IdleTimeout:  2 * time.Minute,
		ReturnWait:   5 * time.Minute,
		FloorHold:    time.Minute,
		SpotPriceAge: time.Hour,
	}
}

// demandWindow is the longest any market's headroom looks back.
func (p Policy) demandWindow() time.Duration {
	var out time.Duration
	for _, r := range slices.Concat([]MarketReserve{p.Spot, p.OnDemand}, slices.Collect(maps.Values(p.GPU))) {
		out = max(out, r.Warm.Lead, r.Warm.Memory, r.Stopped.Lead, r.Stopped.Memory)
	}
	return out
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
