package compute

import (
	"testing"

	"github.com/AmbientWare/lazycloud/internal/cpu"
)

func cpuGiB(millis cpu.Millis, memGiB int64) FleetCapacity {
	return FleetCapacity{CPUMillis: millis, MemoryBytes: memGiB * gib}
}

// A CPU market's large-shape reserve fits at least the default, so small
// recent work can't shrink it below what an 8 CPU request needs; recent
// work only raises it, and the disks it holds, up to the cap.
func TestLargeShapeReserveFitsAtLeastTheDefault(t *testing.T) {
	t.Parallel()
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
