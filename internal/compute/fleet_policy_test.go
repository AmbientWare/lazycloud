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

// Demand within a lead time is the most capacity its arrivals held running
// at once, as steady demand less the largest batch and that batch as the
// burst: steady work keeps about its rate, short containers give their room
// back, and arrivals before the lead count for nothing.
func TestDemandIsWhatArrivalsHoldRunningAtOnce(t *testing.T) {
	now, one, batch := offerNow, cpuGiB(1000, 2), DefaultPolicy().Batch
	every := func(n int, gap, ago, runs time.Duration) []Arrival {
		var out []Arrival
		for i := range n {
			a := Arrival{At: now.Add(-ago - time.Duration(i)*gap), Shape: one}
			if runs > 0 {
				a.Stopped = a.At.Add(runs)
			}
			out = append(out, a)
		}
		return out
	}
	cases := []struct {
		name          string
		arrivals      []Arrival
		steady, burst FleetCapacity
	}{
		{"quiet", nil, FleetCapacity{}, FleetCapacity{}},
		{"steady", every(6, 10*time.Second, 0, 0), one.Times(5), one},
		{"a burst", every(20, 0, 0, 0), FleetCapacity{}, one.Times(20)},
		{"short containers give their room back", every(12, 10*time.Second, 0, 3*time.Second), FleetCapacity{}, one},
		{"arrivals before the lead", every(20, 0, 3*time.Minute, 0), FleetCapacity{}, FleetCapacity{}},
	}
	for _, c := range cases {
		if d := DemandOf(c.arrivals, now, 2*time.Minute, batch); d.Steady != c.steady || d.Burst != c.burst {
			t.Errorf("%s: %+v, want steady %+v burst %+v", c.name, d, c.steady, c.burst)
		}
	}
}

// A layer remembers the most its demand reached for its memory, whatever
// the arrivals a pass reads: less demand keeps the peak, more replaces it
// and restarts the memory, and it is forgotten once the memory passes; a
// target is at least its floor.
func TestADemandPeakIsRememberedForItsMemory(t *testing.T) {
	memory, big, small := time.Hour, cpuGiB(20_000, 40), cpuGiB(4000, 8)
	var p DemandPeak
	p = p.after(big, offerNow, memory)
	if p = p.after(small, offerNow.Add(30*time.Minute), memory); p.Demand != big {
		t.Fatalf("less demand replaced the peak: %+v", p)
	}
	if p.after(FleetCapacity{}, offerNow.Add(memory), memory).Demand != (FleetCapacity{}) {
		t.Fatal("a peak outlived its memory")
	}
	if p = p.after(big.Times(2), offerNow.Add(50*time.Minute), memory); p.after(FleetCapacity{}, offerNow.Add(memory+30*time.Minute), memory).Demand != big.Times(2) {
		t.Fatal("a higher peak did not restart the memory")
	}
	target := HeadroomTarget{Floor: cpuGiB(1000, 4)}
	if got := target.Of(FleetCapacity{}); got != target.Floor {
		t.Errorf("quiet target %+v, want the floor", got)
	}
	if got := target.Of(big); got != big {
		t.Errorf("target %+v, want the demand", got)
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
