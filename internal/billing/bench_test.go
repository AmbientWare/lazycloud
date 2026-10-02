package billing

import (
	"io"
	"log/slog"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// fleet inserts accounts accounts, each owning one workspace with perAccount
// ready containers that became ready at ready, on hosts that reported now.
func fleet(b *testing.B, f *fixture, accounts, perAccount int, ready time.Time) {
	b.Helper()
	_, err := f.pool.Exec(b.Context(), `
with owner as (
    insert into users (email) select 'owner-' || n || '@example.test' from generate_series(1, $1) n returning id
), numbered as (
    select id, row_number() over () as n from owner
), ws as (
    insert into workspaces (name) select 'ws-' || n from numbered returning id, name
), member as (
    insert into workspace_members (workspace_id, user_id, role)
    select ws.id, numbered.id, 'owner' from ws join numbered on ws.name = 'ws-' || numbered.n
), app as (
    insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id, workspace_id
), wl as (
    insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id, app_id
), rel as (
    insert into releases (workload_id, version, spec, spec_digest, source_sha256)
    select id, 1, '{}'::jsonb, sha256('spec'), sha256('src') from wl returning id, workload_id
), host as (
    insert into hosts (name, token_hash, state, cpu_millis, memory_bytes, last_seen_at)
    select 'h' || n, sha256(('h' || n)::bytea), 'online', 1 << 20, 1::bigint << 50, now() from generate_series(1, 100) n
    returning id
), hosts as (
    select id, row_number() over () as n from host
)
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
select app.workspace_id, rel.id, 'ready', hosts.id, 1, 1000, 1 << 30, $3, $3
from rel
join wl on wl.id = rel.workload_id
join app on app.id = wl.app_id
cross join generate_series(1, $2) c
join hosts on hosts.n = 1 + (c % 100)`, accounts, perAccount, ready)
	if err != nil {
		b.Fatal(err)
	}
}

// BenchmarkMetering10kContainers measures the passes 10,000 running
// containers of 1,000 accounts cost: the first, which writes every
// container's entries and cursor; a steady one between grid boundaries,
// which only refreshes accrued cost; and the rollup of every due account.
func BenchmarkMetering10kContainers(b *testing.B) {
	f := &fixture{pool: dbtest.New(b)}
	f.billing = NewBilling(f.pool, Config{}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	now := time.Now().UTC()
	// Ready far enough back that each container closes a few quarters.
	fleet(b, f, 1_000, 10, now.Truncate(MeteringPeriod).Add(-40*time.Minute))
	ctx := b.Context()

	b.Run("first pass", func(b *testing.B) {
		for b.Loop() {
			b.StopTimer()
			if _, err := f.pool.Exec(ctx, "truncate usage_cursors, ledger_entries, billing_hours, metering_state"); err != nil {
				b.Fatal(err)
			}
			b.StartTimer()
			result, err := f.billing.Meter(ctx)
			if err != nil || result.Containers != 10_000 {
				b.Fatalf("meter: %+v %v", result, err)
			}
		}
	})
	b.Run("steady pass", func(b *testing.B) {
		for b.Loop() {
			if _, err := f.billing.Meter(ctx); err != nil {
				b.Fatal(err)
			}
		}
	})
	b.Run("rollup of 1000 accounts", func(b *testing.B) {
		for b.Loop() {
			b.StopTimer()
			if _, err := f.pool.Exec(ctx, "update billing_balances set due = true"); err != nil {
				b.Fatal(err)
			}
			b.StartTimer()
			result, err := f.billing.Rollup(ctx)
			for err == nil && result.More {
				result, err = f.billing.Rollup(ctx)
			}
			if err != nil {
				b.Fatal(err)
			}
		}
	})
	var entries int
	if err := f.pool.QueryRow(ctx, "select count(*) from ledger_entries").Scan(&entries); err != nil {
		b.Fatal(err)
	}
	b.ReportMetric(float64(entries), "entries")
}
