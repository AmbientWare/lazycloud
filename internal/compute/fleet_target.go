package compute

import (
	"slices"
	"strings"
	"time"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

// LoadPeak is a held capacity: the most a target reached, and when it last
// reached it.
type LoadPeak struct {
	Load FleetCapacity `json:"load"`
	At   time.Time     `json:"at"`
}

// hold is the peak once a pass computes value: the peak as it was while
// value matches it, value itself when it rises above the peak or the window
// has passed since the peak was last raised, else the peak, raised where
// value exceeds it. A target so held rises at once and falls only once the
// window has passed since the market last needed what it holds. At is when
// it last changed.
func (p LoadPeak) hold(value FleetCapacity, now time.Time, window time.Duration) LoadPeak {
	switch {
	case value == p.Load:
		return p
	case value.Covers(p.Load) || now.Sub(p.At) >= window:
		return LoadPeak{Load: value, At: now}
	}
	return LoadPeak{Load: p.Load.Upper(value), At: p.At}
}

// MarketHolds are a market's two held targets: the running capacity its
// serving and starting hosts keep, held for the keep window, and the
// stopped capacity its reserves keep, held for the cost horizon; and
// ShortSince, since when the kept hosts have fallen short of the running
// target, zero while they cover it. The published plan carries them from
// one pass to the next.
type MarketHolds struct {
	Running    LoadPeak  `json:"running"`
	Stopped    LoadPeak  `json:"stopped"`
	ShortSince time.Time `json:"short_since"`
}

// marketTarget is what one market holds in a pass, from the snapshot alone.
type marketTarget struct {
	m ReserveMarket
	// load is what runs on the market's hosts, CPU work packed into GPU
	// hosts aside, which is the CPU Spot market's, and what the market's
	// pending containers ask for.
	load FleetCapacity
	// want is the running target this pass's load and headroom ask for;
	// holds.Running is want held for the keep window. arriving is set in
	// the CPU Spot market while arrivals within the arrival window set its
	// headroom: the next arrivals land on it, so it is bought at once.
	want     FleetCapacity
	arriving bool
	holds    MarketHolds
	// headroom is what want holds beyond the load, and floor the least of
	// it; empty in a market without a floor. Headroom is held in parts of
	// part, the largest shape where the market keeps one, else the floor:
	// each part fits one start of that shape, so free room split across
	// hosts in fragments smaller than a start is not headroom. heldParts
	// split the held running target's headroom, wantParts this pass's.
	headroom, floor, part FleetCapacity
	heldParts, wantParts  []FleetCapacity
	// largest is the shape one of the market's reserves fits, with a disk
	// slot for each of largestDisks; empty for none. loaded is set while
	// the share of load, not the floor beside the largest shape, sets the
	// stopped target.
	largest      FleetCapacity
	largestDisks int
	loaded       bool
}

// targets are every market the policy, a host or a pending container names,
// each with its target.
func (ps *pass) targets(items []coverItem) []*marketView {
	markets := ps.p.Markets()
	known := len(markets)
	add := func(m ReserveMarket) {
		if !slices.Contains(markets, m) {
			markets = append(markets, m)
		}
	}
	for _, h := range ps.hosts {
		add(h.market())
	}
	for _, it := range items {
		add(it.market)
	}
	slices.SortStableFunc(markets[known:], func(a, b ReserveMarket) int { return strings.Compare(a.String(), b.String()) })
	views := make([]*marketView, 0, len(markets))
	for _, m := range markets {
		v := &marketView{marketTarget: ps.target(m, items), retired: map[HostID]bool{}}
		ps.byMarket[m] = v
		views = append(views, v)
	}
	return views
}

// target sets market m's targets from its load, what its hosts' containers
// reserve and what its pending containers ask for. The running target is
// the load plus its headroom: the larger of the floor and a share of load,
// and in the Spot CPU market at least the floor plus what arrived within
// the arrival window, held for the keep window. The stopped target is a
// share of load, at least the floor beside the largest shape one reserve
// must fit, held for the cost horizon.
func (ps *pass) target(m ReserveMarket, items []coverItem) marketTarget {
	t := marketTarget{m: m, load: ps.marketLoad(m)}
	largest, disks := ps.s.Recent[m], ps.s.RecentDisks[m]
	for _, it := range items {
		if it.market == m {
			t.load = t.load.Plus(reservedShape(it.need))
			largest, disks = largest.Upper(reservedShape(it.need)), max(disks, it.need.Disks)
		}
	}
	r := ps.p.Reserve(m)
	// A market with a floor keeps room for the largest shape, within the
	// cap, that started within the warm window, and at least the floor.
	floor := r.Warm.Floor
	if !floor.Empty() {
		floor = floor.Upper(ps.s.Started[m].Lower(ps.p.LargestShape.Cap))
	}
	warm := floor.Upper(t.load.Percent(r.Warm.LoadPercent))
	if m.spotCPU() && r.Warm != (HeadroomTarget{}) {
		arrivals := floor.Plus(ps.s.Arrived[m])
		t.arriving = !ps.s.Arrived[m].Empty() && arrivals.Covers(warm)
		warm = warm.Upper(arrivals)
	}
	t.want = t.load.Plus(warm)
	t.holds.Running = ps.s.Holds[m].Running.hold(t.want, ps.s.Now, ps.p.Keep)
	t.headroom, t.floor = warm, floor
	stopped := r.Stopped.Of(t.load)
	if r.FitLargest {
		t.largest, t.largestDisks = ps.p.LargestShape.of(m, largest, disks, ps.s.Offers.Catalog)
		stopped = stopped.Upper(r.Stopped.Floor.Plus(t.largest))
	}
	t.holds.Stopped = ps.s.Holds[m].Stopped.hold(stopped, ps.s.Now, ps.p.CostHorizon)
	t.holds.ShortSince = ps.s.Holds[m].ShortSince
	t.part = t.largest
	if t.part.Empty() {
		t.part = t.floor.Lower(ps.p.LargestShape.Cap)
	}
	t.heldParts = split(t.holds.Running.Load.Minus(t.load).Clamp(), t.part)
	t.wantParts = split(t.headroom, t.part)
	t.loaded = r.FitLargest && !r.Stopped.Floor.Plus(t.largest).Covers(t.holds.Stopped.Load)
	return t
}

// marketLoad is what runs on market m's hosts: Spot-tolerant work
// borrowing an on-demand host holds that host, so the on-demand market
// keeps it for the keep window once the work ends. CPU work packed into a
// GPU host is the CPU Spot market's: it never holds the GPU host, and the
// CPU Spot market keeps room for it once the host leaves and it is queued
// again.
func (ps *pass) marketLoad(m ReserveMarket) FleetCapacity {
	var load FleetCapacity
	for _, h := range ps.hosts {
		if h.market() == m {
			load = load.Plus(h.Load.Minus(h.Packed))
		}
		if m.spotCPU() {
			load = load.Plus(h.Packed)
		}
	}
	return load
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

// unplaced are the parts that do not fit, first fit, in the rooms.
func unplaced(rooms, parts []FleetCapacity) []FleetCapacity {
	rooms = slices.Clone(rooms)
	var out []FleetCapacity
	for _, part := range parts {
		i := slices.IndexFunc(rooms, func(r FleetCapacity) bool { return r.Covers(part) })
		if i < 0 {
			out = append(out, part)
			continue
		}
		rooms[i] = rooms[i].Minus(part)
	}
	return out
}

// placesAll reports whether the parts fit, first fit, in the rooms.
func placesAll(rooms, parts []FleetCapacity) bool { return len(unplaced(rooms, parts)) == 0 }

// total sums capacities.
func total(parts []FleetCapacity) FleetCapacity {
	var out FleetCapacity
	for _, p := range parts {
		out = out.Plus(p)
	}
	return out
}
