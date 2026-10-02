package execution

import (
	"errors"
	"strconv"
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

// explainAs explains query under plan_cache_mode mode in a transaction it
// rolls back: "auto" plans with the arguments, "force_generic_plan" as a
// prepared statement reused across arguments may.
func explainAs(t *testing.T, pool *pgxpool.Pool, mode, query string, args ...any) string {
	t.Helper()
	var plan string
	err := pgx.BeginFunc(t.Context(), pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(t.Context(), "select set_config('plan_cache_mode', $1, true)", mode); err != nil {
			return err
		}
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

// buffers is the most shared buffers any node of a plan touched while
// executing, which a walk over many rows dominates. Planning is left out.
func buffers(plan string) int {
	most := 0
	for _, line := range strings.Split(plan, "\n") {
		line = strings.TrimSpace(line)
		if line == "Planning:" || strings.HasPrefix(line, "Planning Time:") {
			break
		}
		if !strings.HasPrefix(line, "Buffers:") {
			continue
		}
		total := 0
		for _, field := range strings.Fields(line) {
			for _, kind := range []string{"hit=", "read="} {
				if n, ok := strings.CutPrefix(field, kind); ok {
					if v, err := strconv.Atoi(n); err == nil {
						total += v
					}
				}
			}
		}
		most = max(most, total)
	}
	return most
}

// scan is one recurring statement and its arguments.
type scan struct {
	name  string
	query string
	args  []any
}

// costAt explains every scan with custom and generic plans and returns the
// most buffers either touched. With a positive limit it fails a plan in which
// one node over tasks, attempts or containers returns or filters out more
// than limit rows: a scan of every row, or of a whole backlog or history.
func costAt(t *testing.T, pool *pgxpool.Pool, scans []scan, label string, limit int) map[string]int {
	t.Helper()
	cost := map[string]int{}
	for _, s := range scans {
		for _, mode := range []string{"auto", "force_generic_plan"} {
			plan := explainAs(t, pool, mode, s.query, s.args...)
			cost[s.name] = max(cost[s.name], buffers(plan))
			t.Logf("%s, %s, %s: %s", label, s.name, mode, summary(plan))
			if limit <= 0 {
				continue
			}
			if node, rows := widestRead(plan); rows > limit {
				t.Errorf("%s: %s (%s) reads %d rows in %q:\n%s", label, s.name, mode, rows, node, plan)
			}
		}
	}
	return cost
}

// widestRead is the plan node over tasks, attempts or containers that
// returned or filtered out the most rows, and that count.
func widestRead(plan string) (string, int) {
	var node, widest string
	most := 0
	for _, line := range strings.Split(plan, "\n") {
		trimmed := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(line), "->"))
		if strings.Contains(trimmed, "(actual") {
			node = ""
			for _, table := range []string{" on tasks", " on attempts", " on containers"} {
				if strings.Contains(trimmed, table) {
					node = trimmed
				}
			}
			if node != "" {
				rows := planNumber(trimmed, "rows=") * max(planNumber(trimmed, "loops="), 1)
				if rows > most {
					most, widest = rows, node
				}
			}
			continue
		}
		if node != "" && strings.HasPrefix(trimmed, "Rows Removed by") {
			_, n, _ := strings.Cut(trimmed, ": ")
			if v, err := strconv.Atoi(n); err == nil && v > most {
				most, widest = v, node
			}
		}
	}
	return widest, most
}

// planNumber reads the integer part of the number after key in line.
func planNumber(line, key string) int {
	_, rest, ok := strings.Cut(line, key)
	if !ok {
		return 0
	}
	end := strings.IndexFunc(rest, func(r rune) bool { return r < '0' || r > '9' })
	if end < 0 {
		end = len(rest)
	}
	v, err := strconv.Atoi(rest[:end])
	if err != nil {
		return 0
	}
	return v
}

// constantCost fails each scan whose buffers grew from small to large by
// more than slack and to more than double: a read that walks the grown rows
// exceeds both many times, while index upkeep on a write varies by a few
// dozen pages with how the B-trees happen to split.
func constantCost(t *testing.T, small, large map[string]int, slack int, grown string) {
	t.Helper()
	for name, before := range small {
		if after := large[name]; after > before+slack && after > 2*before {
			t.Errorf("%s reads %d buffers after %s, %d before: its cost follows the grown rows", name, after, grown, before)
		}
	}
}

// Recurring planner, recovery and idle passes, and each host report, read
// partial indexes of live rows, so their cost does not grow with retained
// history.
func TestRecurringScansReadOnlyLiveRows(t *testing.T) {
	pool := dbtest.New(t)
	f := newRelease(t, pool, `{"autoscaler": {"max_containers": 5}}`)
	host := newHost(t, pool)
	queueTasks(t, pool, f, 20, 0)
	busy := readyContainer(t, pool, f, host, time.Minute)
	attemptOn(t, pool, f, busy, 0)
	readyContainer(t, pool, f, host, time.Minute)

	scans := []scan{
		{"planning releases", planningReleases, []any{uuid.Nil, planningBatch}},
		{"queued available", queuedAvailable, []any{[]uuid.UUID{f.release}, int64(5)}},
		{"serving releases", servingReleases, []any{int32(startFailureLimit), uuid.Nil, int32(planningBatch)}},
		{"pod releases", podReleases, []any{int32(startFailureLimit), uuid.Nil, int32(planningBatch)}},
		{"live work", hasLiveWork, nil},
		{"idle containers", lockIdleContainers, []any{f.release, 0.0, 5}},
		{"idle instances", drainIdleInstances, []any{int32(100)}},
		{"unserved instances", drainUnservedInstances, nil},
		{"unserved pending instances", stopUnservedPendingInstances, nil},
		{"snapshot candidates", automaticSnapshotCandidates, nil},
		{"stale snapshots", failStaleSnapshots, []any{SnapshotDeadline.Seconds()}},
		{"stale filesystem images", failStaleFilesystemImages, []any{PublishDeadline.Seconds()}},
		{"overdue attempts", overdueAttempts, []any{time.Time{}, uuid.Nil, recoveryBatch}},
		{"stuck starts", stuckStartingContainers, []any{StartTimeout.Seconds(), time.Time{}, uuid.Nil, recoveryBatch}},
		{"host containers", liveContainersOnHost, []any{host}},
		{"ended attempts on host", endedAttemptsOnHost, []any{&host, cancelResendWindow.Seconds()}},
	}
	addHistory(t, pool, f, host, 1_000)
	exec(t, pool, "analyze")
	small := costAt(t, pool, scans, "1,000 finished tasks", 0)
	addHistory(t, pool, f, host, 20_000)
	exec(t, pool, "analyze")
	large := costAt(t, pool, scans, "21,000 finished tasks", 500)
	constantCost(t, small, large, 20, "20,000 more finished tasks and 2,000 more stopped containers")
}
