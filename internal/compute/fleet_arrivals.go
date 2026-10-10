package compute

import (
	"slices"
	"time"
)

// Arrival is a container a market placed: when it was created and what it
// reserves.
type Arrival struct {
	At    time.Time
	Shape FleetCapacity
}

// Demand is a market's recent demand as its headroom covers it: Steady is
// what arrived at a steady rate within the lead time, those arrivals less
// their largest batch, and Burst is the largest batch within the memory.
type Demand struct {
	Steady, Burst FleetCapacity
}

// DemandOf is the demand arrivals make at now. A batch is arrivals less
// than batch.Quiet apart and at most batch.Max long, as purchases wait for
// them; a batch's size counts only its arrivals within the window it is
// measured in.
func DemandOf(arrivals []Arrival, now time.Time, lead, memory time.Duration, batch BatchWindow) Demand {
	sorted := slices.SortedStableFunc(slices.Values(arrivals), func(a, b Arrival) int { return a.At.Compare(b.At) })
	var total, leadBatch, memoryBatch, largestLead, largest FleetCapacity
	var start, last time.Time
	for _, a := range sorted {
		age := now.Sub(a.At)
		if age >= lead && age >= memory {
			continue
		}
		if a.At.Sub(last) >= batch.Quiet || a.At.Sub(start) >= batch.Max {
			largestLead, largest = largestLead.Upper(leadBatch), largest.Upper(memoryBatch)
			start, leadBatch, memoryBatch = a.At, FleetCapacity{}, FleetCapacity{}
		}
		last = a.At
		if age < lead {
			total, leadBatch = total.Plus(a.Shape), leadBatch.Plus(a.Shape)
		}
		if age < memory {
			memoryBatch = memoryBatch.Plus(a.Shape)
		}
	}
	largestLead, largest = largestLead.Upper(leadBatch), largest.Upper(memoryBatch)
	return Demand{Steady: total.Minus(largestLead).Clamp(), Burst: largest}
}
