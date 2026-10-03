package execution

import (
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// The statements every completion or claim runs cost the same with 100 or
// 10,000 tasks queued behind them, over 20,000 finished ones: they reach
// tasks by key or through a bounded prefix of a partial index, never by
// walking the backlog. That holds both with fresh statistics and with
// statistics taken before the queue filled, which is what production has
// between autovacuum runs and what made the planner walk the queued index.
func TestPerCompletionCostIgnoresBacklog(t *testing.T) {
	pool := dbtest.New(t)
	f := newRelease(t, pool, `{"autoscaler": {"max_containers": 4, "tasks_per_container": 4}}`)
	host := newHost(t, pool)
	container := readyContainer(t, pool, f, host, time.Minute)
	upstream := attemptOn(t, pool, f, container, time.Second)
	var upstreamTask uuid.UUID
	if err := pool.QueryRow(t.Context(), "select task_id from attempts where id = $1", upstream).Scan(&upstreamTask); err != nil {
		t.Fatal(err)
	}
	ids := []uuid.UUID{upstreamTask}
	addHistory(t, pool, f, host, 20_000)
	queueTasks(t, pool, f, 100, 0)
	var queued []uuid.UUID
	if err := pool.QueryRow(t.Context(),
		"select array_agg(id) from (select id from tasks where status = 'queued' limit 2) q").Scan(&queued); err != nil {
		t.Fatal(err)
	}

	scans := []scan{
		{"queued dependents", lockQueuedDependents, []any{ids}},
		{"dependent closure", lockDependentClosure, []any{ids}},
		{"queued batch with dependents", lockQueuedWithDependents, []any{f.release, int32(10)}},
		{"satisfy dependencies", satisfyDependencies, []any{queued, ids}},
		{"fail tasks", failTasks, []any{[]byte(`{}`), queued}},
		{"fail queued tasks of release", failQueuedTasksOfRelease, []any{[]byte(`{}`), queued}},
		{"planning releases", planningReleases, []any{uuid.Nil, planningBatch}},
		{"claim", claimQueuedTasks, []any{f.release, int32(4), int64(64 << 20), container, 60.0}},
		{"next queued", nextQueuedAt, []any{f.release}},
		{"live work", hasLiveWork, nil},
	}
	exec(t, pool, "analyze")
	small := costAt(t, pool, scans, "100 queued", 0)
	queueTasks(t, pool, f, 9_900, 0)
	stale := costAt(t, pool, scans, "10,000 queued, statistics from 100", 500)
	constantCost(t, small, stale, 20, "9,900 more queued tasks with statistics from before them")
	exec(t, pool, "analyze")
	fresh := costAt(t, pool, scans, "10,000 queued", 500)
	constantCost(t, small, fresh, 20, "9,900 more queued tasks")
}
