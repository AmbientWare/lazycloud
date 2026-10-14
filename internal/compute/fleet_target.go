package compute

import (
	"slices"
	"strings"
	"time"
)

// LoadPeak is the most load a market held and when it last reached it.
type LoadPeak struct {
	Load FleetCapacity `json:"load"`
	At   time.Time     `json:"at"`
}

// after is the load peak once a pass sees load: the stored one while
// within the horizon, raised to load, and restarted when load reaches it.
// Its time is to the minute, so load held at its peak republishes it at
// most once a minute.
func (p LoadPeak) after(load FleetCapacity, now time.Time, horizon time.Duration) LoadPeak {
	if now.Sub(p.At) >= horizon {
		p = LoadPeak{}
	}
	switch {
	case load.Empty():
		return p
	case load.Covers(p.Load):
		return LoadPeak{Load: load, At: now.Truncate(time.Minute)}
	}
	return LoadPeak{Load: p.Load.Upper(load), At: p.At}
}

// views are every market the policy, a host or a pending container names,
// each with its target.
func (ps *pass) views(items []coverItem) []*marketView {
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

// marketTarget is what one market holds in a pass, from the snapshot alone.
type marketTarget struct {
	m     ReserveMarket
	load  FleetCapacity
	slots []warmSlot
	// warm is the warm target the slots hold, but a build's.
	warm FleetCapacity
	// stopped is the reserve target purchases keep, a share of the load,
	// and peakShare the same share of the peak, which reserves woken for a
	// burst return to.
	stopped, peakShare FleetCapacity
	// peak is the market's load peak; while it holds, the reserves a burst
	// bought or woke wait for the next one.
	peak LoadPeak
	// largest is the shape one of the market's reserves fits, with a disk
	// slot for each of largestDisks; empty for none. loaded is set while
	// the share of load, not the floor beside the largest shape, sets the
	// stopped target.
	largest      FleetCapacity
	largestDisks int
	loaded       bool
}

// target sets market m's targets from its current load, what its hosts'
// containers reserve and what its pending containers ask for: its warm
// slots, with one of the shape of its builds while they run in a CPU
// market, since a GPU host held warm for builds costs far more, and its
// stopped target: its share of load, and at least its floor beside the
// largest shape one reserve must fit.
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
	// The Spot market keeps warm at least the work that came at a steady
	// rate.
	t.warm = r.Warm.Of(t.load)
	if m.spotCPU() && r.Warm != (HeadroomTarget{}) {
		t.warm = t.warm.Upper(r.Warm.Floor.Plus(ps.s.Arrived[m]))
	}
	t.slots = ps.p.slots(r.Warm, t.warm)
	if build := ps.s.Builds[m]; !build.Empty() && m.GPU == "" && r.Warm != (HeadroomTarget{}) {
		t.slots = append(t.slots, warmSlot{shape: build.Lower(ps.p.LargestShape.Cap), kind: slotBuild})
	}
	t.peak = ps.s.Peaks[m].after(t.load, ps.s.Now, ps.p.CostHorizon)
	t.stopped, t.peakShare = r.Stopped.Of(t.load), r.Stopped.Of(t.peak.Load)
	if r.FitLargest {
		t.largest, t.largestDisks = ps.p.LargestShape.of(m, largest, disks)
		floor := r.Stopped.Floor.Plus(t.largest)
		t.loaded = !floor.Covers(t.stopped)
		t.stopped, t.peakShare = t.stopped.Upper(floor), t.peakShare.Upper(floor)
	}
	return t
}

// marketLoad is what market m's work holds on running hosts: its hosts'
// containers, less what Spot-tolerant work borrows on them and CPU work
// packs on them, for a Spot market what its work borrows on on-demand
// hosts of its GPU model, or of CPU, and for the CPU Spot market what its
// work packs on GPU hosts.
func (ps *pass) marketLoad(m ReserveMarket) FleetCapacity {
	var load FleetCapacity
	for _, h := range ps.hosts {
		switch {
		case h.market() == m:
			load = load.Plus(h.Load.Minus(h.Lent).Minus(h.Packed))
		case m == reserveMarketOf(MarketSpot, h.GPU):
			load = load.Plus(h.Lent)
		}
		if m.spotCPU() {
			load = load.Plus(h.Packed)
		}
	}
	return load
}

// slotItems are every market's warm slots as cover items, largest first.
func slotItems(views []*marketView) []coverItem {
	var out []coverItem
	for _, v := range views {
		for _, slot := range v.slots {
			out = append(out, coverItem{slot: true, kind: slot.kind, need: slotNeed(v.m, slot.shape), market: v.m, wait: -1})
		}
	}
	slices.SortStableFunc(out, func(a, b coverItem) int { return size(b.need) - size(a.need) })
	return out
}
