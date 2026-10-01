package observability_test

import (
	"os"
	"slices"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

// TestMeasureAggregatesOverHistory times the 24-hour aggregates with 30
// days of history: 300,000 tasks, 20,000 of them in the last day, and
// 30,000 stopped containers. Set LAZYCLOUD_MEASURE=1 to run it.
func TestMeasureAggregatesOverHistory(t *testing.T) {
	if os.Getenv("LAZYCLOUD_MEASURE") == "" {
		t.Skip("set LAZYCLOUD_MEASURE=1 to measure")
	}
	f := newFixture(t, `{}`)
	f.exec1(`insert into tasks (id, workspace_id, workload_id, release_id, status, max_attempts, created_at, started_at, finished_at)
select uuidv7(-make_interval(secs => s)), $1, $2, $3, 'succeeded', 1,
       now() - make_interval(secs => s), now() - make_interval(secs => s) + interval '20 ms',
       now() - make_interval(secs => s) + interval '20 ms' + make_interval(secs => (s % 7) / 10.0)
from (select case when g <= 20000 then g * 4.32 else 86400 + (g - 20000) * 8.94 end as s
      from generate_series(1, 300000) g) x`, uuid.UUID(f.workspace), f.workload, f.release)
	f.exec1(`insert into containers (id, workspace_id, release_id, state, slots, cpu_millis, memory_bytes, created_at, assigned_at, stopped_at, stop_reason)
select uuidv7(-make_interval(secs => s)), $1, $2, 'stopped', 1, 1000, 1 << 30,
       now() - make_interval(secs => s), now() - make_interval(secs => s), now() - make_interval(secs => s) + interval '5 minutes', 'stopped'
from (select g * 86.4 as s from generate_series(1, 30000) g) x`, uuid.UUID(f.workspace), f.release)
	f.exec1("analyze")
	member := []identity.Workspace{{ID: f.workspace, Name: "acme", Role: identity.RoleOwner}}
	measure := func(name string, run func() error) {
		var times []time.Duration
		for range 20 {
			began := time.Now()
			if err := run(); err != nil {
				t.Fatal(err)
			}
			times = append(times, time.Since(began))
		}
		slices.Sort(times)
		t.Logf("%s: p50 %s p95 %s", name, times[10], times[19])
	}
	ctx := t.Context()
	measure("task metrics, 24 h", func() error {
		m, err := f.obs.TaskMetrics(ctx, f.workspace, nil, nil, observability.TaskFilter{})
		if err == nil && (m.Total < 19900 || m.Total > 20000) {
			t.Fatalf("total %d", m.Total)
		}
		return err
	})
	measure("task activity, 24 hourly buckets", func() error {
		_, err := f.obs.TaskActivity(ctx, f.workspace, observability.RangeQuery{}, nil)
		return err
	})
	measure("deployment performance, 24 h", func() error {
		_, err := f.obs.DeploymentPerformance(ctx, f.workspace, f.workload, observability.RangeQuery{})
		return err
	})
	measure("account container starts, 24 h", func() error {
		_, err := f.obs.AccountActivity(ctx, member, observability.ActivityQuery{Measure: apitypes.ActivityMeasureContainers, Limit: 5})
		return err
	})
	measure("account CPU allocation, 24 h", func() error {
		_, err := f.obs.AccountActivity(ctx, member, observability.ActivityQuery{Measure: apitypes.ActivityMeasureCpu, Limit: 5})
		return err
	})
}
