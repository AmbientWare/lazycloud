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

// MarketHolds are what a market's targets carry from one pass to the next:
// the running and stopped targets, when work took the floor from the hosts
// the market keeps, zero while one keeps it free, and when the market last
// grew, after which it keeps every host it holds for its window. A market
// without them, as after a deploy, holds what it holds now.
type MarketHolds struct {
	Running    Hold      `json:"running"`
	Stopped    Hold      `json:"stopped"`
	FloorTaken time.Time `json:"floor_taken"`
	Grew       time.Time `json:"grew"`
}

func (h Hold) equal(o Hold) bool { return h.Value == o.Value && h.At.Equal(o.At) }

// equal reports whether the holds are the same, growth to the step, so a
// growing market republishes at most once a step.
func (h MarketHolds) equal(o MarketHolds) bool {
	return h.Running.equal(o.Running) && h.Stopped.equal(o.Stopped) && h.FloorTaken.Equal(o.FloorTaken) &&
		h.Grew.Truncate(holdStep).Equal(o.Grew.Truncate(holdStep))
}

// MarketTarget is what a market keeps. Running is the capacity its serving,
// starting and requested hosts hold: its load, running and pending, and
// beyond it its floor and what arrived within the arrival window, less the
// largest batch, or its share of load, whichever is larger, held for the
// market's window. A build that ended within the build window adds its
// shape, and Floor, the room one of those hosts keeps free, is the
// market's floor or that shape, so the next build starts at once. Stopped
// is what its reserves hold: its stopped floor or share of load, and at
// least that floor beside Largest, the shape one reserve fits with a disk
// slot for each of LargestDisks, held for the cost horizon.
type MarketTarget struct {
	Load, Running, Floor, Stopped, Largest FleetCapacity
	LargestDisks                           int
	// Want is the running target now, before the hold.
	Want  FleetCapacity
	Holds MarketHolds
}

// targetInputs are what a market's target follows: its load, its arrivals
// less the largest batch, the largest recent build, the largest shape its
// work reserved and the most disks one attached, what its hosts hold now,
// and the holds the last pass carried, nil for none.
type targetInputs struct {
	load, arrived, build, largest FleetCapacity
	disks                         int
	held                          FleetCapacity
	carried                       *MarketHolds
}

// target is market m's target at now.
func (p Policy) target(m ReserveMarket, in targetInputs, now time.Time) MarketTarget {
	r := p.Reserve(m)
	t := MarketTarget{Load: in.load, Floor: r.Running.Floor}
	var build FleetCapacity
	if !t.Floor.Empty() {
		build = in.build.Lower(p.LargestShape.Cap)
		t.Floor = t.Floor.Upper(build)
	}
	stopped := r.Stopped.Of(in.load)
	if r.FitLargest {
		t.Largest, t.LargestDisks = p.LargestShape.of(m, in.largest, in.disks)
		stopped = stopped.Upper(r.Stopped.Floor.Plus(t.Largest))
	}
	holds := MarketHolds{Running: Hold{Value: in.held, At: now}}
	if in.carried != nil {
		holds = *in.carried
	}
	t.Want = in.load
	if !t.Floor.Empty() {
		t.Want = t.Want.Plus(r.Running.Floor.Plus(in.arrived).Upper(in.load.Percent(r.Running.LoadPercent))).Plus(build)
	}
	t.Holds = MarketHolds{
		Running:    holds.Running.after(t.Want, now, p.Window(m)),
		Stopped:    holds.Stopped.after(stopped, now, p.CostHorizon),
		FloorTaken: holds.FloorTaken, Grew: holds.Grew,
	}
	t.Running, t.Stopped = t.Holds.Running.Value, t.Holds.Stopped.Value
	return t
}
