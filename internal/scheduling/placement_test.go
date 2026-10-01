package scheduling

import (
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

func newScheduling(pool *pgxpool.Pool) *Scheduling {
	return NewScheduling(pool, slog.New(slog.NewTextHandler(io.Discard, nil)))
}

func exec(t testing.TB, pool *pgxpool.Pool, sql string, args ...any) {
	t.Helper()
	if _, err := pool.Exec(t.Context(), sql, args...); err != nil {
		t.Fatalf("%s: %v", sql, err)
	}
}

type release struct{ workspace, id uuid.UUID }

// newRelease inserts a workspace with one app, workload and release.
func newRelease(t testing.TB, pool *pgxpool.Pool) release {
	t.Helper()
	var r release
	err := pool.QueryRow(t.Context(), `
with ws as (insert into workspaces (name) values ('ws-' || substr(md5(random()::text), 1, 8)) returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'app', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, '{}', sha256('spec'), sha256('src') from wl returning id)
select ws.id, rel.id from ws, rel`).Scan(&r.workspace, &r.id)
	if err != nil {
		t.Fatalf("insert release: %v", err)
	}
	return r
}

func newHost(t testing.TB, pool *pgxpool.Pool, cpu, memory int64, seenAgo time.Duration) uuid.UUID {
	t.Helper()
	var id uuid.UUID
	err := pool.QueryRow(t.Context(), `
insert into hosts (name, token_hash, state, cpu_millis, memory_bytes, last_seen_at)
values ('h', sha256(random()::text::bytea), 'online', $1, $2, now() - make_interval(secs => $3)) returning id`,
		cpu, memory, seenAgo.Seconds()).Scan(&id)
	if err != nil {
		t.Fatalf("insert host: %v", err)
	}
	return id
}

// pendingContainer inserts a pending container of r created age ago.
func pendingContainer(t testing.TB, pool *pgxpool.Pool, r release, cpu, memory int64, age time.Duration) uuid.UUID {
	t.Helper()
	var id uuid.UUID
	err := pool.QueryRow(t.Context(), `
insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes, created_at)
values ($1, $2, 'pending', 1, $3, $4, now() - make_interval(secs => $5)) returning id`,
		r.workspace, r.id, cpu, memory, age.Seconds()).Scan(&id)
	if err != nil {
		t.Fatalf("insert container: %v", err)
	}
	return id
}

func place(t testing.TB, s *Scheduling) PlacementResult {
	t.Helper()
	result, err := s.Place(t.Context())
	if err != nil {
		t.Fatalf("place: %v", err)
	}
	return result
}

const gib = 1 << 30

func TestPlacementRoundRobinsWorkspacesAndLeavesShortfallPending(t *testing.T) {
	pool := dbtest.New(t)
	s := newScheduling(pool)
	host := newHost(t, pool, 4000, 8*gib, 0)
	newHost(t, pool, 64000, 64*gib, time.Minute) // silent past the liveness timeout
	busy, quiet := newRelease(t, pool), newRelease(t, pool)
	var busyIDs, quietIDs []uuid.UUID
	for i := range 4 {
		busyIDs = append(busyIDs, pendingContainer(t, pool, busy, 1000, gib, time.Duration(10-i)*time.Minute))
	}
	for i := range 2 {
		quietIDs = append(quietIDs, pendingContainer(t, pool, quiet, 1000, gib, time.Duration(2-i)*time.Minute))
	}

	if result := place(t, s); result.Assigned != 4 {
		t.Fatalf("assigned %d, want 4 (the live host's capacity)", result.Assigned)
	}
	want := map[uuid.UUID]bool{busyIDs[0]: true, busyIDs[1]: true, quietIDs[0]: true, quietIDs[1]: true}
	rows, err := pool.Query(t.Context(), "select id, state, host_id from containers")
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	for rows.Next() {
		var id uuid.UUID
		var state string
		var assigned *uuid.UUID
		if err := rows.Scan(&id, &state, &assigned); err != nil {
			t.Fatal(err)
		}
		switch {
		case want[id] && (state != "starting" || assigned == nil || *assigned != host):
			t.Errorf("container %s is %s on %v, want starting on the live host", id, state, assigned)
		case !want[id] && state != "pending":
			t.Errorf("container %s is %s; later turns of the busy workspace stay pending", id, state)
		}
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
}

func TestPackChoosesTheTightestFit(t *testing.T) {
	roomy := compute.HostCapacity{Host: compute.HostID(uuid.New()), Kind: compute.KindPlatform, CPUMillis: 8000, MemoryBytes: 8 * gib, FreeCPUMillis: 8000, FreeMemoryBytes: 8 * gib}
	tight := compute.HostCapacity{Host: compute.HostID(uuid.New()), Kind: compute.KindPlatform, CPUMillis: 8000, MemoryBytes: 8 * gib, FreeCPUMillis: 2000, FreeMemoryBytes: 2 * gib}
	pending := []PendingContainersRow{
		{ID: uuid.New(), CpuMillis: 2000, MemoryBytes: 2 * gib},
		{ID: uuid.New(), CpuMillis: 2000, MemoryBytes: 2 * gib},
		{ID: uuid.New(), CpuMillis: 9000, MemoryBytes: gib},
	}
	ids, hosts := pack([]compute.HostCapacity{roomy, tight}, pending)
	if len(ids) != 2 || hosts[0] != uuid.UUID(tight.Host) || hosts[1] != uuid.UUID(roomy.Host) {
		t.Fatalf("placed %v on %v; want the first on the tight host, the second on the roomy one and the oversized one nowhere", ids, hosts)
	}
}

// Placers racing over the same hosts never reserve more than a host holds:
// the placement lock serializes them over one capacity snapshot.
func TestConcurrentPlacersNeverOversubscribeHosts(t *testing.T) {
	pool := dbtest.New(t)
	s := newScheduling(pool)
	for range 3 {
		newHost(t, pool, 4000, 4*gib, 0)
	}
	releases := []release{newRelease(t, pool), newRelease(t, pool), newRelease(t, pool)}

	for round := range 10 {
		for i := range 30 {
			pendingContainer(t, pool, releases[i%3], int64(500+(i%3)*500), int64(256<<20)*int64(1+i%4), time.Duration(i)*time.Second)
		}
		var wg sync.WaitGroup
		for range 4 {
			wg.Go(func() {
				if _, err := s.Place(t.Context()); err != nil {
					t.Error(err)
				}
			})
		}
		wg.Wait()

		var over int
		if err := pool.QueryRow(t.Context(), `
select count(*) from (
    select h.id from hosts h join containers c on c.host_id = h.id and c.state <> 'stopped'
    group by h.id
    having sum(c.cpu_millis) > max(h.cpu_millis) or sum(c.memory_bytes) > max(h.memory_bytes)
) over_capacity`).Scan(&over); err != nil {
			t.Fatal(err)
		}
		if over != 0 {
			t.Fatalf("round %d: %d hosts oversubscribed", round, over)
		}
		exec(t, pool, "update containers set state = 'stopped', stopped_at = now()")
	}
}

// A host that host loss marks lost while placement runs receives nothing:
// placement either commits first, so host loss stops the container, or skips
// the host.
func TestPlacementSkipsAHostMarkedLostConcurrently(t *testing.T) {
	pool := dbtest.New(t)
	s := newScheduling(pool)
	host := newHost(t, pool, 4000, 8*gib, 0)
	container := pendingContainer(t, pool, newRelease(t, pool), 1000, gib, time.Minute)

	loss, err := pool.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = loss.Rollback(t.Context()) }()
	if _, err := loss.Exec(t.Context(), "update hosts set last_seen_at = now() - interval '1 minute' where id = $1", host); err != nil {
		t.Fatal(err)
	}
	if marked, err := compute.MarkLost(t.Context(), loss, compute.HostID(host)); err != nil || !marked {
		t.Fatalf("mark lost: %v, %v", marked, err)
	}

	done := make(chan PlacementResult, 1)
	go func() {
		result, err := s.Place(t.Context())
		if err != nil {
			t.Error(err)
		}
		done <- result
	}()
	var result PlacementResult
	placed := false
	for deadline := time.Now().Add(5 * time.Second); !placed; {
		select {
		case result = <-done:
			placed = true
			continue
		default:
		}
		var waiting int
		if err := pool.QueryRow(t.Context(), `
select count(*) from pg_stat_activity where datname = current_database() and wait_event_type = 'Lock'`).Scan(&waiting); err != nil {
			t.Fatal(err)
		}
		if waiting > 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("placement neither finished nor waited for the host")
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := loss.Commit(t.Context()); err != nil {
		t.Fatal(err)
	}
	if !placed {
		result = <-done
	}
	var state string
	var assigned *uuid.UUID
	if err := pool.QueryRow(t.Context(), "select state, host_id from containers where id = $1", container).Scan(&state, &assigned); err != nil {
		t.Fatal(err)
	}
	if result.Assigned != 0 || state != "pending" || assigned != nil {
		t.Fatalf("assigned %d; container %s on %v, want pending off the lost host", result.Assigned, state, assigned)
	}
}
