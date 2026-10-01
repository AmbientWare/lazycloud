package observability_test

import (
	"os"
	"slices"
	"testing"
	"time"
)

// TestMeasureChangeTriggerCost times submits with the change triggers on
// and off: single-task submits and 2,000-task batches. Set
// LAZYCLOUD_MEASURE=1 to run it.
func TestMeasureChangeTriggerCost(t *testing.T) {
	if os.Getenv("LAZYCLOUD_MEASURE") == "" {
		t.Skip("set LAZYCLOUD_MEASURE=1 to measure")
	}
	f := newFixture(t, `{"max_pending_tasks": 1000000}`)
	run := func(label string) {
		for _, batch := range []int{1, 2000} {
			rounds := 300
			if batch > 1 {
				rounds = 10
			}
			durations := make([]time.Duration, rounds)
			for i := range rounds {
				began := time.Now()
				f.submit(batch)
				durations[i] = time.Since(began)
				if batch == 1 && i%50 == 49 {
					f.exec1("delete from tasks")
				}
			}
			f.exec1("delete from tasks")
			slices.Sort(durations)
			t.Logf("%s: submit of %d: p50 %s p95 %s", label, batch, durations[rounds/2], durations[rounds*95/100])
		}
	}
	// Warm the connection pool and plans before either measurement.
	f.submit(10)
	run("triggers on")
	f.exec1("alter table tasks disable trigger tasks_created")
	run("triggers off")
	f.exec1("alter table tasks enable trigger tasks_created")
	run("triggers on again")
}
