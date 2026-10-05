package compute

import (
	"slices"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

func cpuGiB(millis cpu.Millis, memGiB int64) FleetCapacity {
	return FleetCapacity{CPUMillis: millis, MemoryBytes: memGiB * gib}
}

func TestFleetTargetsKeepTheFloorOrAShareOfLoad(t *testing.T) {
	p := DefaultPolicy()
	cases := []struct {
		name          string
		market        ReserveMarket
		load          FleetCapacity
		warm, stopped FleetCapacity
	}{
		{"quiet on-demand keeps its floors", ReserveMarket{}, FleetCapacity{}, cpuGiB(1000, 4), cpuGiB(3000, 12)},
		{"quiet Spot keeps its floors", ReserveMarket{Preemptible: true}, FleetCapacity{}, cpuGiB(1000, 4), cpuGiB(3000, 12)},
		{"loaded market keeps a share", ReserveMarket{}, cpuGiB(40_000, 80), cpuGiB(10_000, 20), cpuGiB(20_000, 40)},
		{"a share rounds up", ReserveMarket{}, FleetCapacity{CPUMillis: 10_001}, cpuGiB(2501, 4), cpuGiB(5001, 12)},
		{"GPU markets keep a share without a floor", ReserveMarket{GPU: "T4"}, FleetCapacity{}, FleetCapacity{}, FleetCapacity{}},
		{"loaded GPU market", ReserveMarket{GPU: "L4"}, FleetCapacity{CPUMillis: 4000, GPUs: 4}, FleetCapacity{CPUMillis: 1000, GPUs: 1}, FleetCapacity{CPUMillis: 2000, GPUs: 2}},
		{"other cards keep none", ReserveMarket{GPU: "H100"}, FleetCapacity{CPUMillis: 4000, GPUs: 4}, FleetCapacity{}, FleetCapacity{}},
		{"Spot GPU work keeps none", ReserveMarket{Preemptible: true, GPU: "T4"}, FleetCapacity{CPUMillis: 4000, GPUs: 4}, FleetCapacity{}, FleetCapacity{}},
	}
	for _, c := range cases {
		r := p.Reserve(c.market)
		if warm, stopped := r.Warm.Of(c.load), r.Stopped.Of(c.load); warm != c.warm || stopped != c.stopped {
			t.Errorf("%s: warm %+v stopped %+v, want %+v %+v", c.name, warm, stopped, c.warm, c.stopped)
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
