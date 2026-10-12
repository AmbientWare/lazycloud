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
	"github.com/AmbientWare/lazycloud/internal/cpu"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
)

func newScheduling(pool *pgxpool.Pool) *Scheduling {
	return NewScheduling(pool, execution.NewExecution(pool), slog.New(slog.NewTextHandler(io.Discard, nil)))
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
	dbtest.OwnWorkspaces(t, pool)
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

// A container whose disk was just released goes back to the host that held
// it, whose frame cache still holds the disk, and each disk takes one of a
// host's disk slots.
func TestDisksReturnToTheirWarmHostWithinItsSlots(t *testing.T) {
	pool := dbtest.New(t)
	s := newScheduling(pool)
	tight := newHost(t, pool, 2000, 2*gib, 0)
	warm := newHost(t, pool, 8000, 8*gib, 0)
	exec(t, pool, "update hosts set disk_slots = 1")
	r := newRelease(t, pool)
	exec(t, pool, `update releases set spec = '{"disks":[{"name":"root","size_bytes":1073741824,"mount_path":"/"}]}' where id = $1`, r.id)
	var holder uuid.UUID
	if err := pool.QueryRow(t.Context(), `
insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes, host_id, stop_reason, stopped_at)
values ($1, $2, 'stopped', 1, 1000, $3, $4, 'stopped', now()) returning id`, r.workspace, r.id, gib, warm).Scan(&holder); err != nil {
		t.Fatal(err)
	}
	exec(t, pool, `insert into disks (workspace_id, name, size_bytes, holder_container_id, lease_token, released_at)
values ($1, 'root', 1073741824, $2, '\x00', now() - interval '1 minute')`, r.workspace, holder)
	first := pendingContainer(t, pool, r, 1000, gib, 2*time.Minute)
	second := pendingContainer(t, pool, r, 1000, gib, time.Minute)
	third := pendingContainer(t, pool, r, 1000, gib, 0)

	if result := place(t, s); result.Assigned != 2 {
		t.Fatalf("assigned %d, want one per host's disk slot", result.Assigned)
	}
	for id, want := range map[uuid.UUID]*uuid.UUID{first: &warm, second: &tight, third: nil} {
		var host *uuid.UUID
		if err := pool.QueryRow(t.Context(), "select host_id from containers where id = $1", id).Scan(&host); err != nil {
			t.Fatal(err)
		}
		if (host == nil) != (want == nil) || host != nil && *host != *want {
			t.Errorf("container %s went to %v, want %v", id, host, want)
		}
	}
}

// A container that mounts platform volumes goes to a host in its
// workspace bucket's region before a tighter one elsewhere; one that
// mounts none takes the tightest fit.
func TestVolumesPlaceWorkInTheirBucketsRegion(t *testing.T) {
	pool := dbtest.New(t)
	s := newScheduling(pool)
	west := newHost(t, pool, 4000, 4*gib, 0)
	east := newHost(t, pool, 8000, 8*gib, 0)
	exec(t, pool, "update hosts set region = 'us-west-2' where id = $1", west)
	exec(t, pool, "update hosts set region = 'us-east-1' where id = $1", east)
	mounts, plain := newRelease(t, pool), newRelease(t, pool)
	exec(t, pool, `update releases set spec = '{"volumes":[{"name":"models","mount_path":"/models"}]}' where id = $1`, mounts.id)
	exec(t, pool, "insert into workspace_buckets (workspace_id, bucket, region) select id, 'ws-' || id, 'us-east-1' from workspaces where id = $1", mounts.workspace)
	exec(t, pool, "insert into volumes (workspace_id, name, size_bytes) values ($1, 'models', 16000000000)", mounts.workspace)
	reads := pendingContainer(t, pool, mounts, 2000, gib, time.Minute)
	other := pendingContainer(t, pool, plain, 2000, gib, time.Minute)

	if result := place(t, s); result.Assigned != 2 {
		t.Fatalf("assigned %d, want 2", result.Assigned)
	}
	for id, want := range map[uuid.UUID]uuid.UUID{reads: east, other: west} {
		var host *uuid.UUID
		if err := pool.QueryRow(t.Context(), "select host_id from containers where id = $1", id).Scan(&host); err != nil {
			t.Fatal(err)
		}
		if host == nil || *host != want {
			t.Errorf("container %s went to %v, want %v", id, host, want)
		}
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
	ids, hosts, _ := pack([]compute.HostCapacity{roomy, tight}, pending, nil)
	if len(ids) != 2 || hosts[0] != uuid.UUID(tight.Host) || hosts[1] != uuid.UUID(roomy.Host) {
		t.Fatalf("placed %v on %v; want the first on the tight host, the second on the roomy one and the oversized one nowhere", ids, hosts)
	}
}

// Spot-tolerant work takes a Spot host before a tighter on-demand one and
// borrows on-demand room only while an on-demand host still keeps the
// warm floor free; work that cannot run on Spot is placed first, so
// borrowed room never crowds it out. The host holding a container's disk
// wins only among hosts it may take.
func TestSpotTolerantWorkBorrowsOnDemandRoomLast(t *testing.T) {
	host := func(market compute.Market, freeCPU cpu.Millis) compute.HostCapacity {
		return compute.HostCapacity{Host: compute.HostID(uuid.New()), Kind: compute.KindPlatform, Market: market,
			CPUMillis: 4000, MemoryBytes: 16 * gib, FreeCPUMillis: freeCPU, FreeMemoryBytes: 16 * gib}
	}
	work := func(cpus cpu.Millis, tolerant bool) PendingContainersRow {
		return PendingContainersRow{ID: uuid.New(), CpuMillis: cpus, MemoryBytes: gib, Preemptible: tolerant}
	}
	onDemand, spot := host(compute.MarketOnDemand, 1000), host(compute.MarketSpot, 4000)
	diskOn := func(h compute.HostCapacity) PendingContainersRow {
		w := work(1000, true)
		w.DiskHost = (*uuid.UUID)(&h.Host)
		return w
	}
	for _, c := range []struct {
		name    string
		hosts   []compute.HostCapacity
		pending []PendingContainersRow
		want    []int
	}{
		{"a Spot host before a tighter on-demand one", []compute.HostCapacity{host(compute.MarketOnDemand, 2000), host(compute.MarketSpot, 4000)},
			[]PendingContainersRow{work(1000, true)}, []int{1}},
		{"no borrowing the last free floor", []compute.HostCapacity{host(compute.MarketOnDemand, 1000)},
			[]PendingContainersRow{work(1000, true)}, []int{-1}},
		{"borrowing while another host keeps the floor", []compute.HostCapacity{host(compute.MarketOnDemand, 1000), host(compute.MarketOnDemand, 1000)},
			[]PendingContainersRow{work(1000, true), work(1000, true)}, []int{0, -1}},
		{"work that cannot run on Spot first", []compute.HostCapacity{host(compute.MarketOnDemand, 2000)},
			[]PendingContainersRow{work(1000, true), work(2000, false)}, []int{-1, 0}},
		{"a disk's host before a tighter one", []compute.HostCapacity{host(compute.MarketSpot, 2000), spot},
			[]PendingContainersRow{diskOn(spot)}, []int{1}},
		{"no borrowing the last free floor for a disk's host", []compute.HostCapacity{onDemand, spot},
			[]PendingContainersRow{diskOn(onDemand)}, []int{1}},
	} {
		ids, hosts, _ := pack(c.hosts, c.pending, nil)
		placed := map[uuid.UUID]uuid.UUID{}
		for i, id := range ids {
			placed[id] = hosts[i]
		}
		for i, want := range c.want {
			got, ok := placed[c.pending[i].ID]
			if want < 0 && ok || want >= 0 && got != uuid.UUID(c.hosts[want].Host) {
				t.Errorf("%s: container %d on %v, want host %d", c.name, i, got, want)
			}
		}
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

// GPU work placed on a GPU host whose CPU work holds the room it needs
// evicts that work in the same pass until it fits: idle containers first,
// which hold no room while they wait to be replaced, then the busy one
// whose attempt started last, which stops with its task preempted and
// queued again. CPU work it need not evict keeps running.
func TestGPUWorkEvictsIdleCPUWorkFirstThenBusyAndFits(t *testing.T) {
	pool := dbtest.New(t)
	s := newScheduling(pool)
	host := newHost(t, pool, 4000, 16*gib, 0)
	exec(t, pool, "update hosts set provider = 'aws', market = 'spot', gpu_type = 'T4', gpu_count = 2 where id = $1", host)
	cpuWork, gpuWork := newRelease(t, pool), newRelease(t, pool)
	exec(t, pool, `update releases set spec = case when id = $1 then '{"kind": "function", "resources": {"cpu_millis": 1000, "memory_mib": 2048}, "autoscaler": {"max_containers": 3}}'::jsonb
	else '{"kind": "function", "resources": {"cpu_millis": 1000, "memory_mib": 2048, "gpu": ["T4"]}, "autoscaler": {"max_containers": 2}}'::jsonb end where id in ($1, $2)`, cpuWork.id, gpuWork.id)
	// live is a ready CPU container on the host, running an attempt that
	// started ago before now unless ago is zero.
	live := func(ago time.Duration) (container, task uuid.UUID) {
		t.Helper()
		if err := pool.QueryRow(t.Context(), `
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
values ($1, $2, 'ready', $3, 1, 1000, 2 << 30, now(), now()) returning id`, cpuWork.workspace, cpuWork.id, host).Scan(&container); err != nil {
			t.Fatal(err)
		}
		if ago == 0 {
			return container, uuid.Nil
		}
		var attempt uuid.UUID
		if err := pool.QueryRow(t.Context(), `
with task as (insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts, started_at)
              select $1, workload_id, id, 'running', 1, 1, now() from releases where id = $2 returning id)
insert into attempts (task_id, number, container_id, state, started_at, deadline_at)
select task.id, 1, $3, 'running', now() - make_interval(secs => $4), now() + interval '1 hour' from task returning task_id, id`,
			cpuWork.workspace, cpuWork.id, container, ago.Seconds()).Scan(&task, &attempt); err != nil {
			t.Fatal(err)
		}
		exec(t, pool, "update tasks set current_attempt_id = $1 where id = $2", attempt, task)
		return container, task
	}
	idle, _ := live(0)
	longer, _ := live(10 * time.Minute)
	newest, retried := live(time.Minute)
	// 1000m is free: the first needs the idle container's room as well, the
	// second the newest busy one's.
	first := pendingContainer(t, pool, gpuWork, 2000, 4*gib, 2*time.Minute)
	second := pendingContainer(t, pool, gpuWork, 1000, 2*gib, time.Minute)

	if result := place(t, s); result.Assigned != 2 {
		t.Fatalf("assigned %d of the two GPU containers", result.Assigned)
	}
	for _, c := range []struct {
		id              uuid.UUID
		state           string
		evicted, onHost bool
	}{
		{first, "starting", false, true}, {second, "starting", false, true},
		{idle, "ready", true, true}, {newest, "stopped", true, true}, {longer, "ready", false, true},
	} {
		var state string
		var evicted bool
		var on *uuid.UUID
		if err := pool.QueryRow(t.Context(), "select state, evicted_at is not null, host_id from containers where id = $1", c.id).Scan(&state, &evicted, &on); err != nil {
			t.Fatal(err)
		}
		if state != c.state || evicted != c.evicted || on == nil || *on != host {
			t.Errorf("container %s is %s on %v, evicted %v; want %s, evicted %v", c.id, state, on, evicted, c.state, c.evicted)
		}
	}
	var status string
	var preemptions, maxAttempts int
	if err := pool.QueryRow(t.Context(), "select status, preemptions, max_attempts from tasks where id = $1", retried).Scan(&status, &preemptions, &maxAttempts); err != nil {
		t.Fatal(err)
	}
	if status != "queued" || preemptions != 1 || maxAttempts != 2 {
		t.Errorf("the evicted task is %s after %d preemptions with %d attempts; want queued after one, its retry not spent", status, preemptions, maxAttempts)
	}
	// Planning replaces the idle one, which drains once its replacement is
	// ready.
	if _, err := execution.NewExecution(pool).Plan(t.Context(), slog.New(slog.DiscardHandler)); err != nil {
		t.Fatal(err)
	}
	var replacements int
	if err := pool.QueryRow(t.Context(), "select count(*) from containers where replaces = $1 and state = 'pending'", idle).Scan(&replacements); err != nil {
		t.Fatal(err)
	}
	if replacements != 1 {
		t.Errorf("%d pending replacements of the evicted idle container, want 1", replacements)
	}
}
