package compute

import (
	"cmp"
	"slices"
	"time"
)

// Arrival is a container a market placed: when it was created, when it
// stopped (zero while it runs) and what it reserves.
type Arrival struct {
	At, Stopped time.Time
	Shape       FleetCapacity
}

// Demand is what a market's arrivals within a lead time asked of its
// headroom: Steady is the most capacity they held running at once, less
// their largest batch, and Burst is that batch.
type Demand struct {
	Steady, Burst FleetCapacity
}

// DemandOf is the demand arrivals made within lead of now. A batch is
// arrivals less than batch.Quiet apart and at most batch.Max long, as
// purchases wait for them. Containers that stopped give their room back, so
// a stream of short containers holds about one at a time.
func DemandOf(arrivals []Arrival, now time.Time, lead time.Duration, batch BatchWindow) Demand {
	type event struct {
		at    time.Time
		shape FleetCapacity
		start bool
	}
	var events []event
	var recent []Arrival
	for _, a := range arrivals {
		if now.Sub(a.At) < lead {
			recent = append(recent, a)
			events = append(events, event{a.At, a.Shape, true})
			if !a.Stopped.IsZero() {
				events = append(events, event{a.Stopped, a.Shape, false})
			}
		}
	}
	// A stop at the instant of a start frees its room first.
	slices.SortStableFunc(events, func(a, b event) int {
		return cmp.Or(a.at.Compare(b.at), boolOrder(a.start, b.start))
	})
	var running, peak FleetCapacity
	for _, e := range events {
		if e.start {
			running = running.Plus(e.shape)
			peak = peak.Upper(running)
		} else {
			running = running.Minus(e.shape)
		}
	}
	slices.SortStableFunc(recent, func(a, b Arrival) int { return a.At.Compare(b.At) })
	var largest, current FleetCapacity
	var start, last time.Time
	for _, a := range recent {
		if a.At.Sub(last) >= batch.Quiet || a.At.Sub(start) >= batch.Max {
			largest, start, current = largest.Upper(current), a.At, FleetCapacity{}
		}
		current, last = current.Plus(a.Shape), a.At
	}
	largest = largest.Upper(current)
	return Demand{Steady: peak.Minus(largest).Clamp(), Burst: largest}
}

// DemandPeak is the most a headroom layer's demand within its lead time
// reached within its memory, and when it last reached it, carried from pass
// to pass in the published plan so the memory outlasts the arrivals a pass
// reads. However a burst spreads over its arrivals, its whole demand is
// remembered.
type DemandPeak struct {
	Demand FleetCapacity `json:"demand"`
	At     time.Time     `json:"at"`
}

// after is the peak once a pass sees demand: the stored one while within
// memory, raised to demand, and restarted when demand reaches it. Its time
// is to the minute, so demand held at its peak republishes it at most once
// a minute.
func (p DemandPeak) after(demand FleetCapacity, now time.Time, memory time.Duration) DemandPeak {
	if now.Sub(p.At) >= memory {
		p = DemandPeak{}
	}
	switch {
	case demand.Empty():
		return p
	case demand.Covers(p.Demand):
		return DemandPeak{Demand: demand, At: now.Truncate(time.Minute)}
	}
	return DemandPeak{Demand: p.Demand.Upper(demand), At: p.At}
}

func (p MarketPeaks) equal(o MarketPeaks) bool {
	return p.Warm.Demand == o.Warm.Demand && p.Warm.At.Equal(o.Warm.At) && p.Stopped.Demand == o.Stopped.Demand && p.Stopped.At.Equal(o.Stopped.At)
}
