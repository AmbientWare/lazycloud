package main

import (
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/execution"
)

// A call on a host already serving waits for no capacity; one whose host
// resumed after its container was created waits until the host is ready,
// and one never placed waited for capacity to the end.
func TestSyntheticLatencySplitsCapacityWaitFromPlacement(t *testing.T) {
	at := func(ms int) *time.Time {
		v := time.Date(2026, 10, 3, 0, 0, 0, 0, time.UTC).Add(time.Duration(ms) * time.Millisecond)
		return &v
	}
	for name, c := range map[string]struct {
		latency                         execution.TaskLatency
		admission, capacity, run, total time.Duration
	}{
		"warm host": {
			execution.TaskLatency{Submitted: *at(0), ContainerCreated: at(200), HostReady: at(-60_000), Placed: at(500), Finished: at(1500)},
			500 * time.Millisecond, 0, time.Second, 1500 * time.Millisecond,
		},
		"resumed reserve": {
			execution.TaskLatency{Submitted: *at(0), ContainerCreated: at(200), HostReady: at(12_000), Placed: at(12_500), Finished: at(14_000)},
			700 * time.Millisecond, 11_800 * time.Millisecond, 1500 * time.Millisecond, 14 * time.Second,
		},
		"never placed": {
			execution.TaskLatency{Submitted: *at(0), ContainerCreated: at(200), Finished: at(900_000)},
			200 * time.Millisecond, 899_800 * time.Millisecond, 0, 900 * time.Second,
		},
	} {
		got := splitLatency(c.latency)
		if got.admission != c.admission || got.capacity != c.capacity || got.execution != c.run || got.total != c.total {
			t.Errorf("%s: %+v", name, got)
		}
	}
}
