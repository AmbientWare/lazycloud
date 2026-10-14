package observability_test

import (
	"log/slog"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

// The dashboard's 24-hour aggregates read the day's tasks and containers,
// not the workspace's history: the rows they read stay flat as finished
// tasks and stopped containers pile up behind the day.
func TestDayAggregatesReadTheDayNotTheHistory(t *testing.T) {
	t.Parallel()
	f := newFixture(t, `{}`)
	ctx := t.Context()
	// One connection, so that the rows its queries read are flushed to the
	// statistics on demand.
	cfg := f.pool.Config()
	cfg.MaxConns = 1
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	obs := observability.NewObservability(pool, observability.Config{}, slog.New(slog.DiscardHandler))
	member := []identity.Workspace{{ID: f.workspace, Name: "acme", Role: identity.RoleOwner}}
	aggregates := map[string]func() error{
		"task metrics": func() error {
			_, err := obs.TaskMetrics(ctx, f.workspace, nil, nil, observability.TaskFilter{})
			return err
		},
		"task activity": func() error {
			_, err := obs.TaskActivity(ctx, f.workspace, observability.RangeQuery{}, nil)
			return err
		},
		"deployment performance": func() error {
			_, err := obs.WorkloadPerformance(ctx, f.workspace, f.workload, observability.RangeQuery{})
			return err
		},
		"account container starts": func() error {
			_, err := obs.AccountActivity(ctx, member, observability.ActivityQuery{Measure: apitypes.ActivityMeasureContainers, Limit: 5})
			return err
		},
		"account CPU": func() error {
			_, err := obs.AccountActivity(ctx, member, observability.ActivityQuery{Measure: apitypes.ActivityMeasureCpu, Limit: 5})
			return err
		},
	}
	// read is the rows of tasks and containers that call reads, by
	// sequential scan or index.
	read := func(call func() error) int64 {
		t.Helper()
		total := func() int64 {
			t.Helper()
			if _, err := pool.Exec(ctx, "select pg_stat_force_next_flush()"); err != nil {
				t.Fatal(err)
			}
			var rows int64
			if err := f.pool.QueryRow(ctx, `select coalesce(sum(s.seq_tup_read), 0)::bigint
       + coalesce((select sum(i.idx_tup_read) from pg_stat_user_indexes i where i.relname in ('tasks', 'containers')), 0)::bigint
from pg_stat_user_tables s where s.relname in ('tasks', 'containers')`).Scan(&rows); err != nil {
				t.Fatal(err)
			}
			return rows
		}
		before := total()
		if err := call(); err != nil {
			t.Fatal(err)
		}
		return total() - before
	}
	// history adds n finished tasks and n/10 stopped containers, one to
	// thirty days old.
	history := func(n int) {
		t.Helper()
		f.exec1(`insert into tasks (id, workspace_id, workload_id, release_id, status, max_attempts, created_at, started_at, finished_at)
select uuidv7(-make_interval(secs => s)), $1, $2, $3, 'succeeded', 1,
       now() - make_interval(secs => s), now() - make_interval(secs => s) + interval '20 ms',
       now() - make_interval(secs => s) + interval '1 s'
from (select 86400 + random() * 29 * 86400 as s from generate_series(1, $4::int)) x`, uuid.UUID(f.workspace), f.workload, f.release, n)
		f.exec1(`insert into containers (id, workspace_id, release_id, state, slots, cpu_millis, memory_bytes, created_at, assigned_at, stopped_at, stop_reason)
select uuidv7(-make_interval(secs => s)), $1, $2, 'stopped', 1, 1000, 1 << 30,
       now() - make_interval(secs => s), now() - make_interval(secs => s), now() - make_interval(secs => s) + interval '5 minutes', 'stopped'
from (select 86400 + random() * 29 * 86400 as s from generate_series(1, $3::int)) x`, uuid.UUID(f.workspace), f.release, n/10)
		f.exec1("analyze tasks, containers")
	}
	// The day: 200 tasks and 20 containers.
	f.exec1(`insert into tasks (id, workspace_id, workload_id, release_id, status, max_attempts, created_at, started_at, finished_at)
select uuidv7(-make_interval(secs => g * 400)), $1, $2, $3, 'succeeded', 1, now() - make_interval(secs => g * 400),
       now() - make_interval(secs => g * 400) + interval '20 ms', now() - make_interval(secs => g * 400) + interval '1 s'
from generate_series(1, 200) g`, uuid.UUID(f.workspace), f.workload, f.release)
	for range 20 {
		now := time.Now()
		f.container(f.release, "ready", now, &now, nil, 1000)
	}

	history(2000)
	small := map[string]int64{}
	for name, call := range aggregates {
		small[name] = read(call)
	}
	history(18000)
	for name, call := range aggregates {
		if large := read(call); large > 2*small[name]+100 {
			t.Errorf("%s read %d rows with 2,000 tasks of history and %d with 20,000", name, small[name], large)
		}
	}
}
