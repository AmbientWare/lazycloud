package compute

import (
	"slices"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

func cpuGiB(millis cpu.Millis, memGiB int64) FleetCapacity {
	return FleetCapacity{CPUMillis: millis, MemoryBytes: memGiB * gib}
}

// A target keeps what arrived at a steady rate within its lead time and
// room for the largest batch within its memory, and at least its floor:
// steady work keeps about its rate, a burst keeps room for the next burst
// until its memory passes, and work older than both is forgotten.
func TestHeadroomKeepsTheFloorTheSteadyRateAndTheLargestRecentBurst(t *testing.T) {
	now := offerNow
	one := cpuGiB(1000, 2)
	floor := cpuGiB(1000, 4)
	target := HeadroomTarget{Floor: floor, Lead: time.Minute, Memory: 30 * time.Minute}
	batch := DefaultPolicy().Batch
	every := func(n int, gap, ago time.Duration) []Arrival {
		var out []Arrival
		for i := range n {
			out = append(out, Arrival{At: now.Add(-ago - time.Duration(i)*gap), Shape: one})
		}
		return out
	}
	cases := []struct {
		name     string
		arrivals []Arrival
		want     FleetCapacity
	}{
		{"quiet", nil, floor},
		// Six apart by 10 s within the lead: five steady, and one the
		// largest batch, which the burst counts once.
		{"steady", every(6, 10*time.Second, 0), floor.Upper(one.Times(6))},
		{"a burst within the lead", every(20, 0, 0), floor.Upper(one.Times(20))},
		{"a burst past the lead stays remembered", every(20, 0, 10*time.Minute), floor.Upper(one.Times(20))},
		{"one arrival keeps no more than the floor", every(1, 0, 0), floor},
		{"a burst past its memory is forgotten", every(20, 0, time.Hour), floor},
	}
	for _, c := range cases {
		if got := target.Of(c.arrivals, now, batch); got != c.want {
			t.Errorf("%s: %+v, want %+v", c.name, got, c.want)
		}
	}
	for _, m := range []ReserveMarket{{GPU: "T4"}, {GPU: "L4"}} {
		r := DefaultPolicy().Reserve(m)
		if got := r.Warm.Of(nil, now, batch); !got.Empty() {
			t.Errorf("quiet %s keeps %+v warm, want nothing", m.GPU, got)
		}
	}
	for _, m := range []ReserveMarket{{GPU: "H100"}, {Preemptible: true, GPU: "T4"}} {
		if r := DefaultPolicy().Reserve(m); r != (MarketReserve{}) {
			t.Errorf("%v keeps a reserve %+v, want none", m, r)
		}
	}
}

func TestFleetMarketsAreSpotOnDemandAndTheReservedCards(t *testing.T) {
	want := []ReserveMarket{{Preemptible: true}, {}, {GPU: "A10G"}, {GPU: "L4"}, {GPU: "T4"}}
	if got := DefaultPolicy().Markets(); !slices.Equal(got, want) {
		t.Fatalf("markets %v", got)
	}
}

// A CPU market's large-shape reserve fits at least the default, so small
// recent work can't shrink it below what an 8 CPU request needs; recent
// work only raises it, up to the cap.
func TestLargeShapeReserveFitsAtLeastTheDefault(t *testing.T) {
	s := DefaultPolicy().LargestShape
	spot := ReserveMarket{Preemptible: true}
	cases := []struct {
		name   string
		recent FleetCapacity
		want   FleetCapacity
	}{
		{"no recent work", FleetCapacity{}, s.Default},
		{"small recent work", cpuGiB(1000, 1), s.Default},
		{"larger recent work", cpuGiB(12000, 48), cpuGiB(12000, 48).Upper(s.Default).Lower(s.Cap)},
		{"beyond the cap", cpuGiB(32000, 256), cpuGiB(32000, 256).Lower(s.Cap)},
	}
	for _, c := range cases {
		if got := s.of(spot, c.recent); got != c.want {
			t.Errorf("%s: %+v, want %+v", c.name, got, c.want)
		}
	}
}
