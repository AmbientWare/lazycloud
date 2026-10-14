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
		name           string
		market         ReserveMarket
		load           FleetCapacity
		floor, stopped FleetCapacity
	}{
		{"quiet on-demand keeps its floors", ReserveMarket{}, FleetCapacity{}, cpuGiB(1000, 4), cpuGiB(3000, 12)},
		{"quiet Spot keeps its floors", ReserveMarket{Preemptible: true}, FleetCapacity{}, cpuGiB(1000, 4), cpuGiB(3000, 12)},
		{"loaded market keeps a share stopped", ReserveMarket{}, cpuGiB(40_000, 80), cpuGiB(1000, 4), cpuGiB(20_000, 40)},
		{"a share rounds up", ReserveMarket{}, FleetCapacity{CPUMillis: 10_001}, cpuGiB(1000, 4), cpuGiB(5001, 12)},
		{"GPU markets keep a share without a floor", ReserveMarket{GPU: "T4"}, FleetCapacity{}, FleetCapacity{}, FleetCapacity{}},
		{"loaded GPU market keeps no running headroom", ReserveMarket{GPU: "L4"}, FleetCapacity{CPUMillis: 4000, GPUs: 4}, FleetCapacity{}, FleetCapacity{CPUMillis: 2000, GPUs: 2}},
		{"other cards keep none", ReserveMarket{GPU: "H100"}, FleetCapacity{CPUMillis: 4000, GPUs: 4}, FleetCapacity{}, FleetCapacity{}},
		{"Spot GPU work keeps a stopped share", ReserveMarket{Preemptible: true, GPU: "T4"}, FleetCapacity{CPUMillis: 4000, GPUs: 4}, FleetCapacity{}, FleetCapacity{CPUMillis: 2000, GPUs: 2}},
	}
	for _, c := range cases {
		r := p.Reserve(c.market)
		if floor, stopped := r.Running.Floor, r.Stopped.Of(c.load); floor != c.floor || stopped != c.stopped {
			t.Errorf("%s: floor %+v stopped %+v, want %+v %+v", c.name, floor, stopped, c.floor, c.stopped)
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
