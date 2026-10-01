package compute

import (
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func exec(t testing.TB, pool *pgxpool.Pool, sql string, args ...any) {
	t.Helper()
	if _, err := pool.Exec(t.Context(), sql, args...); err != nil {
		t.Fatalf("%s: %v", sql, err)
	}
}

func newHost(t *testing.T, pool *pgxpool.Pool, seen string) HostID {
	t.Helper()
	var id uuid.UUID
	err := pool.QueryRow(t.Context(), `
insert into hosts (name, token_hash, state, cpu_millis, memory_bytes, last_seen_at)
values ('h', sha256(random()::text::bytea), 'online', 4000, 4::bigint << 30, `+seen+`) returning id`).Scan(&id)
	if err != nil {
		t.Fatalf("insert host: %v", err)
	}
	return HostID(id)
}

// addContainers inserts containers on host in each state, 1000 millicores
// and 1 GiB apiece, and n more stopped ones as history.
func addContainers(t *testing.T, pool *pgxpool.Pool, host HostID, states []string, history int) {
	t.Helper()
	exec(t, pool, `
with ws as (insert into workspaces (name) values ('ws-' || substr(md5(random()::text), 1, 8)) returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, '{}', sha256('spec'), sha256('src') from wl returning id)
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes)
select ws.id, rel.id, s.state, $1, 1, 1000, 1::bigint << 30
from ws, rel, (select unnest($2::text[]) as state union all select 'stopped' from generate_series(1, $3)) s`,
		uuid.UUID(host), states, history)
	exec(t, pool, "analyze")
}

func capacity(t *testing.T, pool *pgxpool.Pool) []HostCapacity {
	t.Helper()
	var hosts []HostCapacity
	err := pgx.BeginFunc(t.Context(), pool, func(tx pgx.Tx) error {
		var err error
		hosts, err = AvailableCapacity(t.Context(), tx)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	return hosts
}

func TestAvailableCapacitySubtractsLiveContainers(t *testing.T) {
	pool := dbtest.New(t)
	host := newHost(t, pool, "now()")
	newHost(t, pool, "now() - interval '1 minute'")
	addContainers(t, pool, host, []string{"starting", "ready", "draining"}, 10_000)

	hosts := capacity(t, pool)
	if len(hosts) != 1 || hosts[0].Host != host {
		t.Fatalf("hosts %v; want only the host that reported within the liveness timeout", hosts)
	}
	if hosts[0].FreeCPUMillis != 1000 || hosts[0].FreeMemoryBytes != 1<<30 {
		t.Fatalf("free %d millicores, %d bytes; want the host minus three live containers", hosts[0].FreeCPUMillis, hosts[0].FreeMemoryBytes)
	}

	var lines []string
	rows, err := pool.Query(t.Context(), "explain (analyze, buffers, costs off) "+availableCapacity, LivenessTimeout.Seconds())
	if err != nil {
		t.Fatal(err)
	}
	if lines, err = pgx.CollectRows(rows, pgx.RowTo[string]); err != nil {
		t.Fatal(err)
	}
	plan := strings.Join(lines, "\n")
	if strings.Contains(plan, "Seq Scan on containers") {
		t.Fatalf("capacity reads stopped containers:\n%s", plan)
	}
	t.Logf("capacity with 10000 stopped containers: %s", lines[len(lines)-1])
}

func TestMarkLostKeepsAHostThatReportedAfterTheScan(t *testing.T) {
	pool := dbtest.New(t)
	host := newHost(t, pool, "now() - interval '1 minute'")
	stale, err := StaleHosts(t.Context(), pool, nil, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(stale) != 1 || stale[0].ID != host {
		t.Fatalf("stale hosts %v, want the silent host", stale)
	}
	exec(t, pool, "update hosts set last_seen_at = now() where id = $1", uuid.UUID(host))

	var marked bool
	err = pgx.BeginFunc(t.Context(), pool, func(tx pgx.Tx) error {
		marked, err = MarkLost(t.Context(), tx, host)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if marked {
		t.Fatal("a host that reported again was marked lost")
	}
}
