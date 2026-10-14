package compute

import "time"

// Hold is a market target held for a window: the highest value it reached
// until the window passes since it last reached it, then the value now.
type Hold struct {
	Value FleetCapacity `json:"value"`
	At    time.Time     `json:"at"`
}

// holdStep is the resolution of a hold's time, the planner's tick: a value
// held steady republishes at most once a step.
const holdStep = 5 * time.Second

// after is the hold once a pass sees v at now.
func (h Hold) after(v FleetCapacity, now time.Time, window time.Duration) Hold {
	if v.Covers(h.Value) || now.Sub(h.At) >= window {
		return Hold{Value: v, At: now.Truncate(holdStep)}
	}
	return Hold{Value: h.Value.Upper(v), At: h.At}
}

// raised is the hold once its market grows to hold v: at least v, its
// window restarted.
func (h Hold) raised(v FleetCapacity, now time.Time) Hold {
	return Hold{Value: h.Value.Upper(v), At: now.Truncate(holdStep)}
}

func (h Hold) equal(o Hold) bool { return h.Value == o.Value && h.At.Equal(o.At) }

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
// the largest batch, held for the market's window. Floor is the room one of
// those hosts keeps free. Stopped is what its reserves hold: its stopped
// floor or share of load, and at least that floor beside Largest, the shape
// one reserve fits with a disk slot for each of LargestDisks, held for the
// cost horizon.
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
		Running: in.carried.Running.after(t.Want, now, p.Window(m)),
		Stopped: in.carried.Stopped.after(stopped, now, p.CostHorizon),
	}
	t.Running, t.Stopped = t.Holds.Running.Value, t.Holds.Stopped.Value
	return t
}
