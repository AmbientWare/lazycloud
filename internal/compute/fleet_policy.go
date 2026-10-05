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
	Largest LargestReserve
}

// LargestReserve is how a market keeps a stopped machine that fits its
// largest recent shape (Policy.LargestShape).
type LargestReserve string

const (
	// LargestNone keeps none.
	LargestNone LargestReserve = ""
	// LargestShared keeps one among the machines that hold the stopped
	// target.
	LargestShared LargestReserve = "shared"
	// LargestApart keeps one beside the machines that hold the stopped
	// target. Work resumes it only when no other reserve fits.
	LargestApart LargestReserve = "apart"
)

// LargestShape sizes a market's LargestReserve machine: the largest CPU,
// memory and GPUs its containers reserved within Window, capped at Cap. A
// CPU market without such containers keeps one of Default; a GPU market
// keeps none.
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
	// IdleTimeout is how long a serving host stays idle before it leaves.
	IdleTimeout time.Duration
	// SpotPriceAge is how old a Spot quote may be and still price a
	// purchase.
	SpotPriceAge time.Duration
	// RegionFailures refusals from distinct offers of one region within
	// RegionFailureWindow rank that region after the others.
	RegionFailures      int
	RegionFailureWindow time.Duration
}

// DefaultPolicy is the policy the planner runs.
func DefaultPolicy() Policy {
	cpuMarket := MarketReserve{
		Warm:    HeadroomTarget{Floor: FleetCapacity{CPUMillis: 1000, MemoryBytes: 4 * gib}, LoadPercent: 25},
		Stopped: HeadroomTarget{Floor: FleetCapacity{CPUMillis: 3000, MemoryBytes: 12 * gib}, LoadPercent: 50},
		Largest: LargestApart,
	}
	card := MarketReserve{Warm: HeadroomTarget{LoadPercent: 25}, Stopped: HeadroomTarget{LoadPercent: 50}, Largest: LargestShared}
	// What a host of 8 CPU and 32 GiB, a size that hibernates, offers; and
	// the cap, what one of 16 CPU and 64 GiB offers, with one card.
	fits := CatalogType{Topology: twoPerCore(16), MemoryBytes: 32 * gib}.Usable(0)
	limit := CatalogType{Topology: twoPerCore(32), MemoryBytes: 64 * gib, GPUCount: 1}.Usable(0)
	return Policy{
		MarginPercent:    30,
		Provision:        300 * time.Second,
		CostHorizon:      time.Hour,
		MaxGrowthActions: 16,
		Spot:             cpuMarket, OnDemand: cpuMarket,
		GPU:            map[string]MarketReserve{"T4": card, "A10G": card, "L4": card},
		LargestShape:   LargestShape{Window: 7 * 24 * time.Hour, Default: fits, Cap: limit},
		IdleTimeout:    5 * time.Minute,
		SpotPriceAge:   time.Hour,
		RegionFailures: 2, RegionFailureWindow: 30 * time.Minute,
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
