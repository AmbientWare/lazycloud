package compute

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// statementCounter counts the statements a pool sends.
type statementCounter struct{ n atomic.Int64 }

func (s *statementCounter) TraceQueryStart(ctx context.Context, _ *pgx.Conn, _ pgx.TraceQueryStartData) context.Context {
	s.n.Add(1)
	return ctx
}

func (*statementCounter) TraceQueryEnd(context.Context, *pgx.Conn, pgx.TraceQueryEndData) {}

var errRollback = errors.New("rollback") //nolint:gochecknoglobals // Test sentinel.

// explainPlan runs EXPLAIN ANALYZE on query in a transaction it rolls back,
// under plan_cache_mode mode.
func explainPlan(t *testing.T, pool *pgxpool.Pool, mode, query string, args ...any) string {
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

// planBuffers is the most shared buffers any node touched while executing.
func planBuffers(plan string) int {
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

// containerRows is the most container or task rows one plan node returned
// or filtered out.
func containerRows(plan string) int {
	most, reading := 0, false
	number := func(line, key string) int {
		_, rest, ok := strings.Cut(line, key)
		if !ok {
			return 0
		}
		end := strings.IndexFunc(rest, func(r rune) bool { return r < '0' || r > '9' })
		if end < 0 {
			end = len(rest)
		}
		v, _ := strconv.Atoi(rest[:end])
		return v
	}
	for _, line := range strings.Split(plan, "\n") {
		trimmed := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(line), "->"))
		if strings.Contains(trimmed, "(actual") {
			reading = strings.Contains(trimmed, " on containers") || strings.Contains(trimmed, " on tasks")
			if reading {
				most = max(most, number(trimmed, "rows=")*max(number(trimmed, "loops="), 1))
			}
			continue
		}
		if reading && strings.HasPrefix(trimmed, "Rows Removed by") {
			_, n, _ := strings.Cut(trimmed, ": ")
			if v, err := strconv.Atoi(n); err == nil {
				most = max(most, v)
			}
		}
	}
	return most
}

func costExec(t *testing.T, pool *pgxpool.Pool, sql string, args ...any) {
	t.Helper()
	if _, err := pool.Exec(t.Context(), sql, args...); err != nil {
		t.Fatalf("%s: %v", sql, err)
	}
}

// The planning pass sends a fixed number of statements, and each snapshot
// read touches about as many buffers with 10,000 pending containers and
// twenty times the finished history as with 2,000: it reads the pending
// batch, live containers per host and the recent window, never the backlog
// or history. Run with -v for the cost table.
func TestPlanningPassCostStaysFlatAsBacklogAndHistoryGrow(t *testing.T) {
	pool := dbtest.New(t)
	counter := &statementCounter{}
	cfg := pool.Config().Copy()
	cfg.ConnConfig.Tracer = counter
	traced, err := pgxpool.NewWithConfig(t.Context(), cfg)
	if err != nil {
		t.Fatal(err)
	}
	defer traced.Close()
	network := func(zone, id string) Network {
		return Network{VPCID: "vpc-1", SecurityGroupID: "sg-1", Subnets: []Subnet{{ID: "subnet-" + zone, Zone: zone, ZoneID: id}}}
	}
	c := NewCompute(traced, nil, Config{Fleet: Fleet{MaxHosts: 1000, IdleTimeout: 5 * time.Minute, Networks: map[string]Network{
		"us-east-2": network("us-east-2a", "use2-az1"), "us-west-1": network("us-west-1b", "usw1-az3"),
	}}})
	// Twenty functions, ten of them scheduled within the horizon, and a
	// hundred serving hosts with four live containers each.
	costExec(t, pool, `
with ws as (insert into workspaces (name) select 'ws-' || n from generate_series(1, 20) n returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id, workspace_id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id, app_id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select wl.id, 1, '{"placement": {"preemptible": false}, "resources": {"cpu_millis": 1000, "memory_mib": 2048}}',
                    sha256('spec'), sha256('src')
             from wl returning id, workload_id)
select count(*) from rel`)
	costExec(t, pool, "update workloads w set active_release_id = r.id from releases r where r.workload_id = w.id")
	costExec(t, pool, `
insert into schedules (workload_id, expression, next_fire_at)
select id, '*/5 * * * *', now() + interval '2 minutes' from workloads order by id limit 10`)
	// The first scheduled function keeps a warm container, so the pass
	// counts its ready ones.
	costExec(t, pool, `
update releases set spec = spec || '{"autoscaler": {"min_containers": 1, "max_containers": 20000}}'
where id = (select active_release_id from workloads order by id limit 1)`)
	costExec(t, pool, `
insert into hosts (name, token_hash, state, last_seen_at, kind, provider, phase, cpu_millis, memory_bytes, market, region,
                   availability_zone, availability_zone_id, instance_type, instance_id, launched_at, session_epoch)
select 'h', sha256(n::text::bytea), 'online', now(), 'platform', 'aws', 'ready', 14400, 54 * (1::bigint << 30), 'on_demand',
       'us-east-2', 'us-east-2a', 'use2-az1', 'm7i.4xlarge', 'i-' || lpad(to_hex(n), 17, '0'), now() - interval '1 hour', 1
from generate_series(1, 100) n`)
	costExec(t, pool, `
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
select a.workspace_id, w.active_release_id, 'ready', h.id, 1, 1000, 2::bigint << 30, now(), now()
from hosts h cross join generate_series(1, 4) n
join lateral (select w.id, w.active_release_id, w.app_id from workloads w order by w.id offset n limit 1) w on true
join apps a on a.id = w.app_id`)
	pending := 0
	history := 0
	grow := func(toPending, toHistory int) {
		costExec(t, pool, `
insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes)
select a.workspace_id, w.active_release_id, 'pending', 1, 1000, 2::bigint << 30
from generate_series(1, $1) n
join lateral (select w.active_release_id, w.app_id from workloads w order by w.id offset n % 20 limit 1) w on true
join apps a on a.id = w.app_id`, toPending-pending)
		costExec(t, pool, `
insert into containers (id, workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, stop_reason,
                        assigned_at, ready_at, stopped_at)
select uuidv7(- interval '2 days'), a.workspace_id, w.active_release_id, 'stopped', null, 1, 1000, 2::bigint << 30, 'stopped',
       now() - interval '2 days', now() - interval '2 days', now() - interval '2 days'
from generate_series(1, $1) n
join lateral (select w.active_release_id, w.app_id from workloads w order by w.id offset n % 20 limit 1) w on true
join apps a on a.id = w.app_id`, toHistory-history)
		costExec(t, pool, `
insert into tasks (id, workspace_id, workload_id, release_id, status, attempt_count, max_attempts, started_at, finished_at)
select uuidv7(- interval '2 days'), a.workspace_id, w.id, w.active_release_id, 'succeeded', 1, 1,
       now() - interval '2 days', now() - interval '2 days' + interval '30 seconds'
from generate_series(1, $1) n
join lateral (select w.id, w.active_release_id, w.app_id from workloads w order by w.id offset n % 20 limit 1) w on true
join apps a on a.id = w.app_id`, toHistory-history)
		costExec(t, pool, `
insert into fleet_activations (kind, instance_type, region, seconds, outcome, at)
select 'provision', 'm7i.4xlarge', 'us-east-2', 240, 'ready', now() - interval '2 days' from generate_series(1, $1)`, (toHistory-history)/10)
		costExec(t, pool, "analyze")
		pending, history = toPending, toHistory
	}
	p := DefaultPolicy()
	now := time.Now()
	scans := []struct {
		name  string
		query string
		args  []any
	}{
		{"hosts", plannerHosts, nil},
		{"pending demand", pendingDemand, []any{int32(demandBatch)}},
		{"recent arrivals", recentArrivals, []any{uuidFloor(now.Add(-p.History))}},
		{"scheduled demand", scheduledDemand, []any{uuidFloor(now.Add(-p.History)), now.Add(p.TotalHorizon())}},
		{"activation stats", activationStats, nil},
		{"cooldowns", plannerCooldowns, []any{p.RegionFailureWindow.Seconds()}},
		{"markets", fleetMarkets, nil},
	}
	type cost struct {
		statements int64
		wall       time.Duration
		buffers    map[string]int
		// plans keeps each scan's costliest plan, to show when it grew.
		plans map[string]string
	}
	measure := func(label string) cost {
		// Undo the last pass, touching only the rows it wrote: rewriting the
		// whole backlog would leave dead versions whose cleanup, and so the
		// heap pages the batch spans, follows autovacuum's timing.
		costExec(t, pool, `update containers set capacity_wait = null, capacity_host_id = null
where state = 'pending' and (capacity_wait is not null or capacity_host_id is not null)`)
		costExec(t, pool, "delete from hosts where phase = 'requested'")
		costExec(t, pool, "delete from fleet_markets")
		// Measure on clean pages and current statistics, whatever
		// autovacuum has done under the load of other tests.
		costExec(t, pool, "vacuum (analyze)")
		counter.n.Store(0)
		start := time.Now()
		result, err := c.Plan(t.Context(), slog.New(slog.DiscardHandler))
		if err != nil || !result.Published {
			t.Fatalf("plan %+v %v, want a reserve pass", result, err)
		}
		out := cost{statements: counter.n.Load(), wall: time.Since(start), buffers: map[string]int{}, plans: map[string]string{}}
		var row []string
		for _, s := range scans {
			for _, mode := range []string{"auto", "force_generic_plan"} {
				plan := explainPlan(t, pool, mode, s.query, s.args...)
				if b := planBuffers(plan); b >= out.buffers[s.name] {
					out.buffers[s.name], out.plans[s.name] = b, mode+"\n"+plan
				}
				if rows := containerRows(plan); rows > demandBatch+100 {
					t.Errorf("%s: %s (%s) reads %d container or task rows:\n%s", label, s.name, mode, rows, plan)
				}
			}
			row = append(row, fmt.Sprintf("%s %d", s.name, out.buffers[s.name]))
		}
		t.Logf("%s: %d statements, %s, requested %d; buffers: %s", label, out.statements, out.wall.Round(time.Millisecond),
			result.Requested, strings.Join(row, ", "))
		return out
	}
	grow(0, 1_000)
	measure("0 pending, 1,000 finished")
	grow(500, 1_000)
	measure("500 pending, 1,000 finished")
	grow(2_000, 1_000)
	small := measure("2,000 pending, 1,000 finished")
	grow(10_000, 20_000)
	// A deep backlog on the warm scheduled function too.
	costExec(t, pool, `
insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes)
select a.workspace_id, w.active_release_id, 'pending', 1, 1000, 2::bigint << 30
from (select active_release_id, app_id from workloads order by id limit 1) w
join apps a on a.id = w.app_id, generate_series(1, 10000)`)
	// And the oldest backlog created in one statement, so thousands of
	// pending containers share one created_at.
	costExec(t, pool, `
insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes, created_at)
select a.workspace_id, w.active_release_id, 'pending', 1, 1000, 2::bigint << 30, now() - interval '1 hour'
from (select active_release_id, app_id from workloads order by id offset 1 limit 1) w
join apps a on a.id = w.app_id, generate_series(1, 5000)`)
	costExec(t, pool, "analyze")
	large := measure("15,000 pending, 10,000 more on a warm cron function, 20,000 finished")
	if large.statements != small.statements {
		t.Errorf("the pass sent %d statements at 10,000 pending and %d at 2,000, want a fixed number", large.statements, small.statements)
	}
	for name, before := range small.buffers {
		if after := large.buffers[name]; after > before+20 && after > 2*before {
			t.Errorf("%s reads %d buffers at 10,000 pending and 20,000 finished, %d at 2,000 and 1,000:\n%s\nbefore:\n%s",
				name, after, before, large.plans[name], small.plans[name])
		}
	}
}
