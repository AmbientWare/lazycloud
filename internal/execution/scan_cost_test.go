package execution

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// addHistory inserts finished work on stopped containers: tasks succeeded
// tasks with one attempt each, ten per container.
func addHistory(t *testing.T, pool *pgxpool.Pool, f releaseFixture, host uuid.UUID, tasks int) {
	t.Helper()
	exec(t, pool, `
with ctr as (
    insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes,
                            stop_reason, assigned_at, ready_at, stopped_at)
    select $1, $2, 'stopped', $3, 1, 1000, 1 << 29, 'stopped', now() - interval '2 days', now() - interval '2 days', now() - interval '1 day'
    from generate_series(1, $5 / 10)
    returning id
), numbered as (
    select id, row_number() over () as n from ctr
), task as (
    insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts, started_at, finished_at)
    select $1, $4, $2, 'succeeded', 1, 1, now() - interval '2 days', now() - interval '1 day'
    from generate_series(1, $5)
    returning id
), task_numbered as (
    select id, row_number() over () as n from task
)
insert into attempts (task_id, number, container_id, state, started_at, deadline_at, finished_at)
select tn.id, 1, c.id, 'succeeded', now() - interval '2 days', now() - interval '1 day', now() - interval '1 day'
from task_numbered tn join numbered c on c.n = (tn.n - 1) / 10 + 1`,
		f.workspace, f.release, host, f.workload, tasks)
}

var errRollback = errors.New("rollback") //nolint:gochecknoglobals // Test sentinel.

// explain runs EXPLAIN ANALYZE on query in a transaction it rolls back.
func explain(t *testing.T, pool *pgxpool.Pool, query string, args ...any) string {
	t.Helper()
	var plan string
	err := pgx.BeginFunc(t.Context(), pool, func(tx pgx.Tx) error {
		rows, err := tx.Query(t.Context(), "explain (analyze, buffers, costs off) "+query, args...)
		if err != nil {
			return err
		}
		lines, err := pgx.CollectRows(rows, pgx.RowTo[string])
		if err != nil {
			return err
		}
		plan = strings.Join(lines, "\n")
		return errRollback
	})
	if !errors.Is(err, errRollback) {
		t.Fatalf("explain: %v", err)
	}
	return plan
}

// summary is a plan's execution time and top-level buffer use.
func summary(plan string) string {
	var buffers, timing string
	for _, line := range strings.Split(plan, "\n") {
		line = strings.TrimSpace(line)
		if buffers == "" && strings.HasPrefix(line, "Buffers:") {
			buffers = line
		}
		if strings.HasPrefix(line, "Execution Time:") {
			timing = line
		}
	}
	return timing + ", " + buffers
}

// Recurring planner and recovery scans read partial indexes of live rows, so
// their cost does not grow with retained history.
func TestRecurringScansReadOnlyLiveRows(t *testing.T) {
	pool := dbtest.New(t)
	f := newRelease(t, pool, `{"autoscaler": {"max_containers": 5}}`)
	host := newHost(t, pool)
	queueTasks(t, pool, f, 20, 0)
	busy := readyContainer(t, pool, f, host, time.Minute)
	attemptOn(t, pool, f, busy, 0)
	readyContainer(t, pool, f, host, time.Minute)

	scans := []struct {
		name  string
		query string
		args  []any
	}{
		{"planning releases", planningReleases, []any{uuid.Nil, planningBatch}},
		{"idle containers", lockIdleContainers, []any{f.release, 0.0, 5}},
		{"overdue attempts", overdueAttempts, []any{time.Time{}, uuid.Nil, recoveryBatch}},
		{"stuck starts", stuckStartingContainers, []any{StartTimeout.Seconds(), time.Time{}, uuid.Nil, recoveryBatch}},
		{"host containers", liveContainersOnHost, []any{host}},
	}
	for _, history := range []int{0, 10_000} {
		if history > 0 {
			addHistory(t, pool, f, host, history)
		}
		exec(t, pool, "analyze")
		for _, scan := range scans {
			plan := explain(t, pool, scan.query, scan.args...)
			t.Logf("%d finished tasks, %s: %s", history, scan.name, summary(plan))
			if history == 0 {
				// A single-page table is cheapest to read whole.
				continue
			}
			for _, table := range []string{"tasks", "attempts", "containers"} {
				if strings.Contains(plan, "Seq Scan on "+table) {
					t.Errorf("%s scans every row of %s:\n%s", scan.name, table, plan)
				}
			}
		}
	}
}
