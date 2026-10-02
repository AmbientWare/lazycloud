package scheduling

import (
	"io"
	"log/slog"
	"slices"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
)

// BenchmarkPlanAndPlace measures one cold scale-up: 2,000 queued tasks on a
// release with max_containers 50, planned and placed across 5 hosts. Each
// iteration starts with no containers. Run with -bench PlanAndPlace
// -benchtime 30x.
func BenchmarkPlanAndPlace(b *testing.B) {
	pool := dbtest.New(b)
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	e := execution.NewExecution(pool)
	s := NewScheduling(pool, logger)
	r := newRelease(b, pool)
	exec(b, pool, `update releases set spec = '{"resources": {"cpu_millis": 1000, "memory_mib": 1024}, "autoscaler": {"max_containers": 50}}' where id = $1`, r.id)
	exec(b, pool, "update workloads set active_release_id = $1 where id = (select workload_id from releases where id = $1)", r.id)
	exec(b, pool, `
insert into tasks (workspace_id, workload_id, release_id, status, max_attempts)
select $1, (select workload_id from releases where id = $2), $2, 'queued', 1 from generate_series(1, 2000)`, r.workspace, r.id)
	for range 5 {
		newHost(b, pool, 16000, 32*gib, 0)
	}
	exec(b, pool, "analyze")

	var planTimes, placeTimes []time.Duration
	for b.Loop() {
		b.StopTimer()
		exec(b, pool, "delete from containers")
		b.StartTimer()
		start := time.Now()
		planned, err := e.Plan(b.Context(), logger)
		if err != nil {
			b.Fatal(err)
		}
		planned1 := time.Now()
		placed, err := s.Place(b.Context())
		if err != nil {
			b.Fatal(err)
		}
		done := time.Now()
		if planned.Created != 50 || placed.Assigned != 50 {
			b.Fatalf("created %d, placed %d; want 50 each", planned.Created, placed.Assigned)
		}
		planTimes = append(planTimes, planned1.Sub(start))
		placeTimes = append(placeTimes, done.Sub(planned1))
	}
	report(b, "plan", planTimes)
	report(b, "place", placeTimes)
}

func report(b *testing.B, name string, times []time.Duration) {
	slices.Sort(times)
	quantile := func(q float64) float64 {
		return float64(times[int(q*float64(len(times)-1))].Microseconds()) / 1000
	}
	b.ReportMetric(quantile(0.5), name+"-p50-ms")
	b.ReportMetric(quantile(0.95), name+"-p95-ms")
	b.ReportMetric(quantile(1), name+"-max-ms")
}
