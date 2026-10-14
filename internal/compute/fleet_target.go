package compute

import "time"

// Hold is a market target that follows its value only once the value has
// stayed on one side of it for that side's window: it rises once the value
// has stayed above it for the rise window and falls once the value has
// stayed below it for the fall window, so a peak shorter than the window
// moves nothing either way. At is when the value last met the hold, or,
// while Rising, when it rose above it.
type Hold struct {
	Value  FleetCapacity `json:"value"`
	At     time.Time     `json:"at"`
	Rising bool          `json:"rising"`
}

// holdStep is the resolution of a hold's time, the planner's tick: a value
// held steady republishes at most once a step.
const holdStep = 5 * time.Second

// after is the hold once a pass sees v at now. A hold never set takes v;
// without a rise window it rises at once.
func (h Hold) after(v FleetCapacity, now time.Time, rise, fall time.Duration) Hold {
	at := now.Truncate(holdStep)
	up := !h.Value.Covers(v)
	switch {
	case h.At.IsZero() || v == h.Value || up && rise <= 0 && v.Covers(h.Value):
		return Hold{Value: v, At: at}
	case up && rise <= 0:
		return Hold{Value: h.Value.Upper(v), At: h.At}
	case up && !h.Rising:
		return Hold{Value: h.Value, At: at, Rising: true}
	case up && now.Sub(h.At) >= rise:
		return Hold{Value: h.Value.Upper(v), At: at}
	case up:
		return h
	case h.Rising:
		// The value fell back before its rise lasted: it met the hold last
		// pass.
		return Hold{Value: h.Value, At: at}
	case now.Sub(h.At) >= fall:
		return Hold{Value: v, At: at}
	}
	return h
}

// raised is the hold once its market grows to hold held while it wants
// want: at least held, its fall window restarted, or, while want stays
// above it, still rising since its rise began.
func (h Hold) raised(held, want FleetCapacity, now time.Time) Hold {
	v, at := h.Value.Upper(held), now.Truncate(holdStep)
	switch {
	case v.Covers(want):
		return Hold{Value: v, At: at}
	case h.Rising:
		return Hold{Value: v, At: h.At, Rising: true}
	}
	return Hold{Value: v, At: at, Rising: true}
}

func (h Hold) equal(o Hold) bool {
	return h.Value == o.Value && h.At.Equal(o.At) && h.Rising == o.Rising
}

// MarketHolds are the running and stopped targets a market carries from
// one pass to the next.
type MarketHolds struct {
	Running Hold `json:"running"`
	Stopped Hold `json:"stopped"`
}

func (h MarketHolds) equal(o MarketHolds) bool {
	return h.Running.equal(o.Running) && h.Stopped.equal(o.Stopped)
}

// MarketTarget is what a market keeps. Running is the capacity its serving,
// starting and requested hosts hold: its load, running and pending, and in
// a CPU market its floor and what arrived within the arrival window less
// the largest batch, followed once that has stayed above or below it for
// the market's window. Floor is the room one of those hosts keeps free.
// Stopped is what its reserves hold: its stopped floor or share of load,
// and at least that floor beside Largest, the shape one reserve fits with a
// disk slot for each of LargestDisks, rising at once and held for the cost
// horizon.
type MarketTarget struct {
	Load, Running, Floor, Stopped, Largest FleetCapacity
	LargestDisks                           int
	// Want is the running target now, before the hold.
	Want  FleetCapacity
	Holds MarketHolds
}

// targetInputs are what a market's target follows: its load, its arrivals
// less the largest batch, the largest shape its work reserved and the most
// disks one attached, and the holds the last pass carried.
type targetInputs struct {
	load, arrived, largest FleetCapacity
	disks                  int
	carried                MarketHolds
}

// target is market m's target at now.
func (p Policy) target(m ReserveMarket, in targetInputs, now time.Time) MarketTarget {
	r := p.Reserve(m)
	t := MarketTarget{Load: in.load, Floor: r.Floor, Want: in.load}
	if !t.Floor.Empty() {
		t.Want = t.Want.Plus(t.Floor).Plus(in.arrived)
	}
	stopped := r.Stopped.Of(in.load)
	if r.FitLargest {
		t.Largest, t.LargestDisks = p.LargestShape.of(m, in.largest, in.disks)
		stopped = stopped.Upper(r.Stopped.Floor.Plus(t.Largest))
	}
	t.Holds = MarketHolds{
		Running: in.carried.Running.after(t.Want, now, p.Window(m), p.Window(m)),
		Stopped: in.carried.Stopped.after(stopped, now, 0, p.CostHorizon),
	}
	t.Running, t.Stopped = t.Holds.Running.Value, t.Holds.Stopped.Value
	return t
}
