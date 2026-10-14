package compute

import (
	"testing"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

func cpuGiB(millis cpu.Millis, memGiB int64) FleetCapacity {
	return FleetCapacity{CPUMillis: millis, MemoryBytes: memGiB * gib}
}

func TestFleetTargetsKeepTheFloorOrAShareOfLoad(t *testing.T) {
	p := DefaultPolicy()
	cases := []struct {
		name    string
		market  ReserveMarket
		load    FleetCapacity
		stopped FleetCapacity
	}{
		{"quiet on-demand keeps its floor", ReserveMarket{}, FleetCapacity{}, cpuGiB(3000, 12)},
		{"quiet Spot keeps its floor", ReserveMarket{Preemptible: true}, FleetCapacity{}, cpuGiB(3000, 12)},
		{"loaded market keeps a share stopped", ReserveMarket{}, cpuGiB(40_000, 80), cpuGiB(20_000, 40)},
		{"a share rounds up", ReserveMarket{}, FleetCapacity{CPUMillis: 10_001}, cpuGiB(5001, 12)},
		{"GPU markets keep a share without a floor", ReserveMarket{GPU: "T4"}, FleetCapacity{}, FleetCapacity{}},
		{"loaded GPU market", ReserveMarket{GPU: "L4"}, FleetCapacity{CPUMillis: 4000, GPUs: 4}, FleetCapacity{CPUMillis: 2000, GPUs: 2}},
		{"other cards keep none", ReserveMarket{GPU: "H100"}, FleetCapacity{CPUMillis: 4000, GPUs: 4}, FleetCapacity{}},
		{"Spot GPU work keeps a stopped share", ReserveMarket{Preemptible: true, GPU: "T4"}, FleetCapacity{CPUMillis: 4000, GPUs: 4}, FleetCapacity{CPUMillis: 2000, GPUs: 2}},
	}
	for _, c := range cases {
		if stopped := p.Reserve(c.market).Stopped.Of(c.load); stopped != c.stopped {
			t.Errorf("%s: stopped %+v, want %+v", c.name, stopped, c.stopped)
		}
	}
}

// A CPU market's large-shape reserve fits at least the default, so small
// recent work can't shrink it below what an 8 CPU request needs; recent
// work only raises it, and the disks it holds, up to the cap.
func TestLargeShapeReserveFitsAtLeastTheDefault(t *testing.T) {
	s := DefaultPolicy().LargestShape
	spot := ReserveMarket{Preemptible: true}
	cases := []struct {
		name      string
		recent    FleetCapacity
		disks     int
		want      FleetCapacity
		wantDisks int
	}{
		{"no recent work", FleetCapacity{}, 0, s.Default, 0},
		{"small recent work", cpuGiB(1000, 1), 3, s.Default, 3},
		{"larger recent work", cpuGiB(12000, 48), 0, cpuGiB(12000, 48).Upper(s.Default).Lower(s.Cap), 0},
		{"beyond the cap", cpuGiB(32000, 256), 64, cpuGiB(32000, 256).Lower(s.Cap), s.CapDisks},
	}
	for _, c := range cases {
		if got, disks := s.of(spot, c.recent, c.disks); got != c.want || disks != c.wantDisks {
			t.Errorf("%s: %+v with %d disks, want %+v with %d", c.name, got, disks, c.want, c.wantDisks)
		}
	}
}
