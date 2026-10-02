package billing

import (
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

var errRollback = errors.New("rollback") //nolint:gochecknoglobals // Test sentinel.

func (f *fixture) explain(query string, args ...any) string {
	f.t.Helper()
	var plan string
	err := pgx.BeginFunc(f.t.Context(), f.pool, func(tx pgx.Tx) error {
		if _, err := tx.Exec(f.t.Context(), "analyze"); err != nil {
			return err
		}
		rows, err := tx.Query(f.t.Context(), "explain (analyze, costs off) "+query, args...)
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
		f.t.Fatal(err)
	}
	return plan
}

// TestMeteringReadsLiveAndRecentContainersOnly keeps the recurring passes
// proportional to running work: neither metering read nor the enforcement
// read scans container or balance history.
func TestMeteringReadsLiveAndRecentContainersOnly(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	host := f.host(time.Now())
	f.exec(`insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes,
	                                assigned_at, ready_at, stopped_at, stop_reason)
		select $1, $2, 'stopped', $3, 1, 1000, 1 << 30, now() - interval '30 days', now() - interval '30 days',
		       now() - interval '29 days', 'stopped'
		from generate_series(1, 20000)`, ws, rel.id, host)
	f.exec(`insert into users (email) select 'bulk' || n || '@example.test' from generate_series(1, 5000) n`)
	f.exec(`insert into billing_accounts (user_id) select id from users on conflict do nothing`)
	f.exec(`insert into billing_balances (user_id, month_started_at, recheck_at, due)
		select user_id, now(), now() + interval '1 day', false from billing_accounts`)
	for _, c := range []struct {
		name, query string
		args        []any
	}{
		{"live containers", "select * from containers where state <> 'stopped' and state in ('ready', 'draining') and ready_at is not null", nil},
		{"stopped containers", "select * from containers where ready_at is not null and stopped_at >= $1", []any{time.Now().Add(-time.Hour)}},
		{"unfunded accounts", "select user_id from billing_balances where live_containers > 0", nil},
		{"due balances", "select user_id from billing_balances where due or recheck_at <= now() limit 1 for update skip locked", nil},
	} {
		plan := f.explain(c.query, c.args...)
		if strings.Contains(plan, "Seq Scan on containers") || strings.Contains(plan, "Seq Scan on billing_balances") {
			t.Errorf("%s scans history:\n%s", c.name, plan)
		}
	}
}

// TestCostsByTaskReadOnlyTheirContainersAttempts keeps the usage page's run
// breakdown proportional to the window's ledger: each entry reads its own
// container's attempts, however much attempt history the account keeps.
func TestCostsByTaskReadOnlyTheirContainersAttempts(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	host := f.host(time.Now())
	base := time.Now().UTC().Truncate(time.Hour).Add(-2 * time.Hour)
	stopped := base.Add(15 * time.Minute)
	c := f.container(containerSpec{workspace: ws, host: host, release: &rel.id, ready: base, stopped: &stopped, cpuMillis: 1000, memoryBytes: 1 << 30})
	// Month-old history: 20,000 finished tasks with attempts in other containers.
	f.exec(`with old as (
    insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at, stopped_at, stop_reason)
    select $1, $2, 'stopped', $3, 1, 1000, 1 << 30, now() - interval '30 days', now() - interval '30 days', now() - interval '29 days', 'stopped'
    from generate_series(1, 200) returning id
), t as (
    insert into tasks (workspace_id, workload_id, release_id, status, max_attempts, finished_at)
    select $1, $4, $2, 'succeeded', 1, now() - interval '29 days' from generate_series(1, 20000) returning id
), numbered as (select id, row_number() over () as n from t), cs as (select id, row_number() over () as n from old)
insert into attempts (task_id, number, container_id, state, started_at, deadline_at, finished_at)
select numbered.id, 1, cs.id, 'succeeded', now() - interval '30 days', now() - interval '29 days', now() - interval '29 days'
from numbered join cs on cs.n = numbered.n % 200 + 1`, ws, rel.id, host, rel.workload)
	f.exec(`with t as (insert into tasks (workspace_id, workload_id, release_id, status, max_attempts, finished_at)
             values ($1, $2, $3, 'succeeded', 1, $5) returning id)
insert into attempts (task_id, number, container_id, state, started_at, deadline_at, finished_at)
select t.id, 1, $4, 'succeeded', $6, $5, $5 from t`, ws, rel.workload, rel.id, c, base.Add(10*time.Minute), base)
	f.meter()

	plan := f.explain(costRows, false, false, int64(0), uuid.Nil, uuid.Nil, uuid.Nil, uuid.Nil, "", uuid.Nil, int32(51),
		owner, base, base.Add(time.Hour), nil, &rel.app, &rel.workload, "", true)
	if strings.Contains(plan, "Seq Scan on attempts") || !strings.Contains(plan, "Index Cond: ((container_id = ") {
		t.Errorf("costs by task scan attempt history:\n%s", plan)
	}
}
