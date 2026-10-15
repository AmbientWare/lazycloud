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

// Percent is p percent of a: CPU and memory rounded up, GPUs rounded down,
// since a fraction of a card is no card. A share of one GPU job holds no
// warm card; a share of four holds one.
func (a FleetCapacity) Percent(p int64) FleetCapacity {
	up := func(v int64) int64 { return (v*p + 99) / 100 }
	return FleetCapacity{CPUMillis: cpu.Millis(up(int64(a.CPUMillis))), MemoryBytes: up(a.MemoryBytes), GPUs: int(int64(a.GPUs) * p / 100)}
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
// on-demand CPU capacity, or one GPU model on Spot or on-demand.
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
	// AwaitReturn counts every host of the market that could return to the
	// reserve as reserve supply, busy ones included.
	AwaitReturn bool
}

// LargestShape sizes the reserve that fits a market's largest shape: the
// largest CPU, memory and GPUs its containers reserved within Window,
// capped at Cap, and the most disks one attached, capped at CapDisks, a
// disk slot each. A CPU market without such containers keeps one of
// Default, a Spot GPU market one of SpotGPU, so the first start after a
// quiet window resumes, and an on-demand GPU market none. Cap also bounds
// a part of headroom.
type LargestShape struct {
	Window                time.Duration
	Default, SpotGPU, Cap FleetCapacity
	CapDisks              int
}

// of is the shape market m's reserve fits and the disks it holds, given
// the largest shape its containers reserved and the most disks one
// attached; empty for none. A GPU reserve holds one card; a CPU reserve
// fits at least the default.
func (s LargestShape) of(m ReserveMarket, largest FleetCapacity, disks int) (FleetCapacity, int) {
	switch {
	case m.GPU == "":
		largest = largest.Upper(s.Default)
	case largest.GPUs > 0:
		largest.GPUs = 1
	case m.Preemptible:
		largest = s.SpotGPU
	default:
		return FleetCapacity{}, 0
	}
	return largest.Lower(s.Cap), min(disks, s.CapDisks)
}

// GPUReserve is the headroom one GPU model keeps for Spot-tolerant work
// and for work that cannot run on Spot.
type GPUReserve struct{ Spot, OnDemand MarketReserve }

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
	// GPU is the reserve per GPU model in each market; a model left out
	// keeps none.
	GPU          map[string]GPUReserve
	LargestShape LargestShape
	// ArrivalWindow is a margin over how long a Spot launch takes to serve
	// work, about a minute: the Spot market keeps warm at least what was
	// placed from arrivals within it, less their largest batch, so work
	// arriving at a steady rate finds room rather than waiting for a
	// launch, while a single burst buys no headroom it cannot use.
	ArrivalWindow time.Duration
	Batch         BatchWindow
	// Keep is how long a market holds its running target once its load
	// falls, so its hosts stay through the gap between bursts, and how long
	// a host resumed from the reserve for a burst counts as due back.
	Keep time.Duration
	// SpotPriceAge is how old a Spot quote may be and still price a
	// purchase.
	SpotPriceAge time.Duration
	// SpotWait is how long Spot-tolerant GPU work waits while no Spot offer
	// of a model it accepts can host it within the purchase margin before it
	// buys a host of its own with the margin set aside, Spot first.
	SpotWait time.Duration
	// QueueTimeout is how long pending fleet work waits for a host that has
	// not launched before execution ends it as a start failure.
	QueueTimeout time.Duration
	// Starts is how long work waits for each way a host starts.
	Starts StartTimes
}

// StartTimes are how long work waits for its first container, measured on
// prod on 2026-10-09 and in the real EC2 checks: 21-25 s after a CPU
// purchase, 13-15 s after a stopped CPU reserve's resume, about 150 s
// after a Spot L4 purchase and about 70 s after a stopped g4dn.xlarge Spot
// reserve's start. EC2 starts a stopped Spot instance only once it
// disables the persistent request, 38-165 s after the stop and 66 s on
// average (2026-10-13); past SpotDisableMax work stops waiting for it.
// Update is what a reserve stopped on an older agent build adds to its
// resume: it downloads, verifies and unpacks the 153 MB release in 5.2 s
// (an m7i.large in us-east-2, 2026-10-13), then restarts into it.
type StartTimes struct {
	Launch, GPULaunch, Resume, GPUResume, SpotDisable, SpotDisableMax, Update time.Duration
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
	// GPU work keeps a share of its load warm in whole cards: one beside
	// every four jobs, none beside one, whose next start resumes the
	// stopped reserve. A Spot GPU host that serves counts as reserve
	// supply: a purchase runs about five minutes of a GPU host for the next
	// concurrent start alone.
	card := MarketReserve{Warm: HeadroomTarget{LoadPercent: 25}, Stopped: HeadroomTarget{LoadPercent: 50}, FitLargest: true}
	spotCard := MarketReserve{Warm: card.Warm, Stopped: card.Stopped, FitLargest: true, AwaitReturn: true}
	gpus := map[string]GPUReserve{}
	for _, model := range []string{"T4", "A10G", "L4"} {
		gpus[model] = GPUReserve{Spot: spotCard, OnDemand: card}
	}
	// What a host of 8 CPU and 32 GiB, a size that hibernates, offers; what
	// one of 4 CPU and 32 GiB with a card offers, so a Spot GPU floor fits a
	// function of 4 CPU and 16 GiB; and the cap, what one of 16 CPU and
	// 64 GiB offers, with one card, and its disk slots.
	fits := CatalogType{Topology: twoPerCore(16), MemoryBytes: 32 * gib}.Usable(0)
	oneCard := CatalogType{Topology: twoPerCore(8), MemoryBytes: 32 * gib, GPUCount: 1}.Usable(0)
	limit := CatalogType{Topology: twoPerCore(32), MemoryBytes: 64 * gib, GPUCount: 1}
	return Policy{
		MarginPercent:    30,
		Provision:        300 * time.Second,
		CostHorizon:      time.Hour,
		MaxGrowthActions: 16,
		Spot:             spotMarket, OnDemand: cpuMarket,
		GPU: gpus,
		LargestShape: LargestShape{
			Window: 7 * 24 * time.Hour, Default: fits, SpotGPU: oneCard, Cap: limit.Usable(0), CapDisks: limit.DiskSlots(),
		},
		ArrivalWindow: 2 * time.Minute,
		Batch:         BatchWindow{Quiet: time.Second, Max: 5 * time.Second},
		Keep:          2 * time.Minute,
		SpotWait:      2 * time.Minute,
		QueueTimeout:  20 * time.Minute,
		SpotPriceAge:  time.Hour,
		Starts: StartTimes{
			Launch: 23 * time.Second, GPULaunch: 150 * time.Second, Resume: 15 * time.Second, GPUResume: 70 * time.Second,
			SpotDisable: 66 * time.Second, SpotDisableMax: 180 * time.Second, Update: 10 * time.Second,
		},
	}
}

// Reserve is the headroom market m keeps.
func (p Policy) Reserve(m ReserveMarket) MarketReserve {
	switch {
	case m.GPU != "" && m.Preemptible:
		return p.GPU[m.GPU].Spot
	case m.GPU != "":
		return p.GPU[m.GPU].OnDemand
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
		markets = append(markets, ReserveMarket{Preemptible: true, GPU: card}, ReserveMarket{GPU: card})
	}
	return markets
}
