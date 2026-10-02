package billing

import (
	"fmt"
	"io"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

type fixture struct {
	t       *testing.T
	pool    *pgxpool.Pool
	billing *Billing
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	pool := dbtest.New(t)
	f := &fixture{t: t, pool: pool, billing: NewBilling(pool, Config{PublicURL: "https://lazycloud.test"}, slog.New(slog.NewTextHandler(io.Discard, nil)))}
	// Containers' measured use is complete through tomorrow unless a test
	// holds the rollup watermark back.
	f.exec("update container_metric_rollup set rolled_through = now() + interval '1 day'")
	return f
}

var names atomic.Int64 //nolint:gochecknoglobals // Unique names across one test binary.

func (f *fixture) exec(sql string, args ...any) {
	f.t.Helper()
	if _, err := f.pool.Exec(f.t.Context(), sql, args...); err != nil {
		f.t.Fatalf("%s: %v", sql, err)
	}
}

// user inserts a user.
func (f *fixture) user() uuid.UUID {
	f.t.Helper()
	var id uuid.UUID
	email := fmt.Sprintf("u%d@example.test", names.Add(1))
	if err := f.pool.QueryRow(f.t.Context(), "insert into users (email) values ($1) returning id", email).Scan(&id); err != nil {
		f.t.Fatal(err)
	}
	return id
}

// workspace inserts a workspace owned by owner.
func (f *fixture) workspace(owner uuid.UUID) uuid.UUID {
	f.t.Helper()
	var id uuid.UUID
	name := fmt.Sprintf("ws%d", names.Add(1))
	err := f.pool.QueryRow(f.t.Context(), `
with ws as (insert into workspaces (name) values ($1) returning id),
     m as (insert into workspace_members (workspace_id, user_id, role) select id, $2, 'owner' from ws)
select id from ws`, name, owner).Scan(&id)
	if err != nil {
		f.t.Fatal(err)
	}
	return id
}

type release struct{ app, workload, id uuid.UUID }

// release inserts an app, a function and its release in workspace.
func (f *fixture) release(workspace uuid.UUID) release {
	f.t.Helper()
	var r release
	err := f.pool.QueryRow(f.t.Context(), `
with app as (insert into apps (workspace_id, name, state) values ($1, 'a' || $2::bigint, 'active') returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id, app_id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, '{}'::jsonb, sha256('spec'), sha256('src') from wl returning id, workload_id)
select wl.app_id, wl.id, rel.id from wl, rel`, workspace, names.Add(1)).Scan(&r.app, &r.workload, &r.id)
	if err != nil {
		f.t.Fatal(err)
	}
	return r
}

// host inserts an online host last seen at lastSeen.
func (f *fixture) host(lastSeen time.Time) uuid.UUID {
	f.t.Helper()
	var id uuid.UUID
	err := f.pool.QueryRow(f.t.Context(), `
insert into hosts (name, token_hash, state, cpu_millis, memory_bytes, last_seen_at)
values ('h', sha256(gen_random_uuid()::text::bytea), 'online', 64000, 1 << 40, $1) returning id`, lastSeen).Scan(&id)
	if err != nil {
		f.t.Fatal(err)
	}
	return id
}

type containerSpec struct {
	workspace, host uuid.UUID
	release         *uuid.UUID
	ready           time.Time
	stopped         *time.Time
	reason          string
	cpuMillis       int64
	memoryBytes     int64
}

// container inserts a container that became ready at spec.ready and is
// draining, or stopped when spec.stopped is set.
func (f *fixture) container(spec containerSpec) uuid.UUID {
	f.t.Helper()
	state := "ready"
	var reason *string
	if spec.stopped != nil {
		state = "stopped"
		r := spec.reason
		if r == "" {
			r = "stopped"
		}
		reason = &r
	}
	var id uuid.UUID
	err := f.pool.QueryRow(f.t.Context(), `
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes,
                        assigned_at, ready_at, stopped_at, stop_reason)
values ($1, $2, $3, $4, 1, $5, $6, $7, $7, $8, $9) returning id`,
		spec.workspace, spec.release, state, spec.host, spec.cpuMillis, spec.memoryBytes, spec.ready, spec.stopped, reason).Scan(&id)
	if err != nil {
		f.t.Fatal(err)
	}
	return id
}

func (f *fixture) meter() MeterResult {
	f.t.Helper()
	result, err := f.billing.Meter(f.t.Context())
	if err != nil {
		f.t.Fatalf("meter: %v", err)
	}
	return result
}

func (f *fixture) rollup() {
	f.t.Helper()
	if _, err := f.billing.Rollup(f.t.Context()); err != nil {
		f.t.Fatalf("rollup: %v", err)
	}
}

type ledgerRow struct {
	start, end time.Time
	version    string
	cost       int64
}

func (f *fixture) ledger(container uuid.UUID) []ledgerRow {
	f.t.Helper()
	rows, err := f.pool.Query(f.t.Context(),
		"select started_at, ended_at, pricing_version, cost_nanos from ledger_entries where source_id = $1 order by started_at", container)
	if err != nil {
		f.t.Fatal(err)
	}
	defer rows.Close()
	var out []ledgerRow
	for rows.Next() {
		var r ledgerRow
		if err := rows.Scan(&r.start, &r.end, &r.version, &r.cost); err != nil {
			f.t.Fatal(err)
		}
		out = append(out, r)
	}
	if err := rows.Err(); err != nil {
		f.t.Fatal(err)
	}
	return out
}

type balanceRow struct {
	balance, accrued, monthSpent int64
	live                         int
	due                          bool
}

func (f *fixture) balance(user uuid.UUID) balanceRow {
	f.t.Helper()
	var b balanceRow
	err := f.pool.QueryRow(f.t.Context(),
		"select balance_nanos, accrued_nanos, month_spent_nanos, live_containers, due from billing_balances where user_id = $1", user).
		Scan(&b.balance, &b.accrued, &b.monthSpent, &b.live, &b.due)
	if err != nil {
		f.t.Fatal(err)
	}
	return b
}

// expected is what shape costs for d under the card in force at at.
func (f *fixture) expected(at time.Time, shape Shape, d time.Duration) int64 {
	f.t.Helper()
	card, err := f.billing.rates.cardAt(at)
	if err != nil {
		f.t.Fatal(err)
	}
	charge, err := card.price(shape, d)
	if err != nil {
		f.t.Fatal(err)
	}
	return charge.Total()
}
