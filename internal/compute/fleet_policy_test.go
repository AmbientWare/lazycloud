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
		got := TargetsFor(p, c.market, TargetInputs{Load: c.load})
		if got.Warm != c.warm || got.Stopped != c.stopped {
			t.Errorf("%s: warm %+v stopped %+v, want %+v %+v", c.name, got.Warm, got.Stopped, c.warm, c.stopped)
		}
	}
}

func TestFleetForecastRaisesTargetsAndTheReserveIsTheRest(t *testing.T) {
	forecast := &MarketForecast{Warm: cpuGiB(16_000, 32), Total: cpuGiB(50_000, 100)}
	got := TargetsFor(DefaultPolicy(), ReserveMarket{}, TargetInputs{Forecast: forecast})
	if got.Warm != forecast.Warm || got.Stopped != cpuGiB(34_000, 68) {
		t.Fatalf("warm %+v stopped %+v", got.Warm, got.Stopped)
	}
}

func TestSpotReserveTakesTheLargestRunningSpotHostsWork(t *testing.T) {
	big := cpuGiB(28_000, 100)
	in := TargetInputs{Load: big.Plus(cpuGiB(1000, 1)), RunningLoads: []FleetCapacity{big, cpuGiB(1000, 1)}}
	if got := TargetsFor(DefaultPolicy(), ReserveMarket{Preemptible: true}, in); !got.Stopped.Covers(big) {
		t.Fatalf("Spot stopped target %+v does not cover the running host's load %+v", got.Stopped, big)
	}
	if got := TargetsFor(DefaultPolicy(), ReserveMarket{}, in); got.Stopped.Covers(big) {
		t.Fatalf("on-demand stopped target %+v was raised to a running host's load", got.Stopped)
	}
}

func TestHibernationTargetIsTheReserveWhenEveryRecentShapeFits(t *testing.T) {
	p := DefaultPolicy()
	hibernating := []FleetCapacity{cpuGiB(7200, 13)}
	cases := []struct {
		name   string
		market ReserveMarket
		shapes []FleetCapacity
		in     []FleetCapacity
		all    bool
	}{
		{"every shape fits", ReserveMarket{}, []FleetCapacity{cpuGiB(4000, 8)}, hibernating, true},
		{"a shape does not fit", ReserveMarket{}, []FleetCapacity{cpuGiB(16_000, 8)}, hibernating, false},
		{"nothing hibernates", ReserveMarket{}, nil, nil, false},
		{"GPU reserves never hibernate", ReserveMarket{GPU: "T4"}, nil, hibernating, false},
	}
	for _, c := range cases {
		got := TargetsFor(p, c.market, TargetInputs{Load: cpuGiB(8000, 16), HibernationShapes: c.in, Shapes: c.shapes})
		if (got.Hibernation == got.Stopped && !got.Stopped.Empty()) != c.all || (!c.all && !got.Hibernation.Empty()) {
			t.Errorf("%s: hibernation %+v of stopped %+v", c.name, got.Hibernation, got.Stopped)
		}
	}
}

func TestFleetMarketsAreSpotOnDemandAndTheReservedCards(t *testing.T) {
	want := []ReserveMarket{{Preemptible: true}, {}, {GPU: "A10G"}, {GPU: "L4"}, {GPU: "T4"}}
	if got := DefaultPolicy().Markets(); !slices.Equal(got, want) {
		t.Fatalf("markets %v", got)
	}
}
