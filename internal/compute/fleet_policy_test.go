package compute

import (
	"slices"
	"testing"
)

func cpuGiB(cpu int64, memGiB int64) FleetCapacity {
	return FleetCapacity{CPUMillis: cpu, MemoryBytes: memGiB * gib}
}

func TestFleetTargetsKeepTheFloorOrAShareOfLoad(t *testing.T) {
	p := DefaultPolicy()
	cases := []struct {
		name          string
		market        ReserveMarket
		load          FleetCapacity
		warm, stopped FleetCapacity
	}{
		{"quiet on-demand keeps its floors", ReserveMarket{}, FleetCapacity{}, cpuGiB(2000, 4), cpuGiB(6000, 12)},
		{"quiet Spot keeps its floors", ReserveMarket{Preemptible: true}, FleetCapacity{}, cpuGiB(2000, 4), cpuGiB(6000, 12)},
		{"loaded market keeps a share", ReserveMarket{}, cpuGiB(40_000, 80), cpuGiB(10_000, 20), cpuGiB(20_000, 40)},
		{"a share rounds up", ReserveMarket{}, FleetCapacity{CPUMillis: 10_001}, cpuGiB(2501, 4), cpuGiB(6000, 12)},
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
