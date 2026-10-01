package billing

import (
	"errors"
	"strings"
	"testing"
	"time"

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
