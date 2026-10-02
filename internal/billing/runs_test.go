package billing

import (
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// Grouping by task gives each run the share of its container's cost that it
// ran for, so the usage page can open it; the time no run used stays with
// the workload, and the rows still add up to the window's total.
func TestCostsByTaskSplitContainerTimeAmongItsRuns(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	base := time.Now().UTC().Truncate(time.Hour).Add(-3 * time.Hour)
	stopped := base.Add(15 * time.Minute)
	host := f.host(time.Now())
	c := f.container(containerSpec{workspace: ws, host: host, release: &rel.id, ready: base, stopped: &stopped, cpuMillis: 1000, memoryBytes: 1 << 30})
	// Two runs of five minutes each, one after the other; the last five
	// minutes the container waits warm.
	var first, second uuid.UUID
	for n, task := range []*uuid.UUID{&first, &second} {
		start := base.Add(time.Duration(n) * 5 * time.Minute)
		if err := f.pool.QueryRow(t.Context(), `
with t as (insert into tasks (workspace_id, workload_id, release_id, status, max_attempts, finished_at)
           select $1, workload_id, id, 'succeeded', 1, $3 from releases where id = $2 returning id)
insert into attempts (task_id, number, container_id, state, started_at, deadline_at, finished_at)
select t.id, 1, $4, 'succeeded', $5, $3, $3 from t returning task_id`,
			ws, rel.id, start.Add(5*time.Minute), c, start).Scan(task); err != nil {
			t.Fatal(err)
		}
	}
	f.meter()

	page, err := f.billing.Costs(t.Context(), owner, CostQuery{
		Start: base, End: base.Add(time.Hour), GroupBy: apitypes.UsageCostGroupTask, App: &rel.app, Limit: 50,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(page.Rows) != 3 {
		t.Fatalf("rows %+v, want two runs and the idle remainder", page.Rows)
	}
	third := page.CostNanos / 3
	var sum int64
	runs := map[uuid.UUID]bool{}
	for _, r := range page.Rows {
		sum += r.CostNanos
		if abs(r.CostNanos-third) > 2 || r.WorkloadId == nil || *r.WorkloadId != rel.workload {
			t.Errorf("row %+v, want a third of %d on the workload", r, page.CostNanos)
		}
		if r.TaskId != nil {
			runs[*r.TaskId] = true
		}
	}
	if !runs[first] || !runs[second] {
		t.Fatalf("runs %v, want %s and %s", runs, first, second)
	}
	if abs(sum-page.CostNanos) > 2 {
		t.Fatalf("rows add to %d, the window costs %d", sum, page.CostNanos)
	}
}
