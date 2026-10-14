package billing

import (
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestMeteringBillsReadyToStoppedOnTheGrid(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	base := time.Now().UTC().Truncate(time.Hour).Add(-3 * time.Hour)
	ready, stopped := base.Add(7*time.Minute+30*time.Second), base.Add(37*time.Minute+10*time.Second)
	host := f.host(time.Now())
	shape := Shape{Owner: OwnerPlatformFleet, Class: ClassAuto, CPUMillis: 2000, MemoryBytes: 4 << 30}
	c := f.container(containerSpec{workspace: ws, host: host, release: &rel.id, ready: ready, stopped: &stopped, cpuMillis: 2000, memoryBytes: 4 << 30})

	f.meter()
	got := f.ledger(c)
	want := []time.Time{ready, base.Add(15 * time.Minute), base.Add(30 * time.Minute), stopped}
	if len(got) != len(want)-1 {
		t.Fatalf("entries = %+v, want %d", got, len(want)-1)
	}
	var total int64
	for n, e := range got {
		if !e.start.Equal(want[n]) || !e.end.Equal(want[n+1]) {
			t.Errorf("entry %d = [%s, %s), want [%s, %s)", n, e.start, e.end, want[n], want[n+1])
		}
		if cost := f.expected(e.start, shape, e.end.Sub(e.start)); e.cost != cost {
			t.Errorf("entry %d costs %d, want %d", n, e.cost, cost)
		}
		total += e.cost
	}
	// 2 CPUs at $0.044/h and 4 GiB at $0.0075/h for 29m40s.
	if wantTotal := f.expected(ready, shape, stopped.Sub(ready)); abs(total-wantTotal) > 3 {
		t.Errorf("total %d, want about %d", total, wantTotal)
	}

	// A second pass, and a cursor lost with it, write nothing twice.
	f.meter()
	f.exec("delete from usage_cursors")
	f.exec("delete from metering_state")
	f.meter()
	if again := f.ledger(c); len(again) != len(got) {
		t.Fatalf("repeated passes wrote %d entries, want %d", len(again), len(got))
	}
	var hourCost int64
	if err := f.pool.QueryRow(t.Context(), "select sum(cost_nanos) from billing_hours where user_id = $1", owner).Scan(&hourCost); err != nil {
		t.Fatal(err)
	}
	if hourCost != total {
		t.Fatalf("hourly totals %d, want the ledger's %d", hourCost, total)
	}
}

func TestMeteringStopsAtTheHostsLastReport(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	now := time.Now().UTC().Truncate(time.Microsecond)
	ready := now.Add(-time.Hour)
	lastSeen := now.Add(-20 * time.Minute)
	shape := Shape{Owner: OwnerPlatformFleet, Class: ClassAuto, CPUMillis: 1000, MemoryBytes: 1 << 30}

	// A live container on a host that went quiet is billed up to the grid
	// before its last report, and the rest of that report as accrued cost.
	live := f.container(containerSpec{workspace: ws, host: f.host(lastSeen), release: &rel.id, ready: ready, cpuMillis: 1000, memoryBytes: 1 << 30})
	// A container stopped by host loss ends at the host's last report, not
	// when the loss was declared.
	lostHost := f.host(lastSeen)
	stopped := now.Add(-5 * time.Minute)
	lost := f.container(containerSpec{workspace: ws, host: lostHost, release: &rel.id, ready: ready, stopped: &stopped, reason: "host_lost", cpuMillis: 1000, memoryBytes: 1 << 30})

	f.meter()
	entries := f.ledger(live)
	closed := lastSeen.Truncate(MeteringPeriod)
	if len(entries) == 0 || !entries[0].start.Equal(ready) || !entries[len(entries)-1].end.Equal(closed) {
		t.Fatalf("live entries %+v, want [%s, %s)", entries, ready, closed)
	}
	lostEntries := f.ledger(lost)
	if !lostEntries[len(lostEntries)-1].end.Equal(lastSeen) {
		t.Fatalf("host-lost container billed to %s, want %s", lostEntries[len(lostEntries)-1].end, lastSeen)
	}
	b := f.balance(owner)
	if want := f.expected(closed, shape, lastSeen.Sub(closed)); abs(b.accrued-want) > 1 || b.live != 1 {
		t.Fatalf("accrued %d over %d live, want %d over 1", b.accrued, b.live, want)
	}

	// The host reports again: the cursor resumes where it stopped.
	f.exec("update hosts set last_seen_at = $1 where id = (select host_id from containers where id = $2)", now, live)
	f.meter()
	entries = f.ledger(live)
	for n := 1; n < len(entries); n++ {
		if !entries[n].start.Equal(entries[n-1].end) {
			t.Fatalf("entries are not contiguous: %+v", entries)
		}
	}
	if end := entries[len(entries)-1].end; !end.Equal(now.Truncate(MeteringPeriod)) {
		t.Fatalf("live container billed to %s, want %s", end, now.Truncate(MeteringPeriod))
	}
}

func TestMeteringSplitsAtPublishedRateChanges(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	change := time.Date(2026, 9, 10, 4, 9, 5, 835918000, time.UTC)
	ready, stopped := change.Add(-2*time.Minute), change.Add(3*time.Minute)
	c := f.container(containerSpec{workspace: ws, host: f.host(time.Now()), release: &rel.id, ready: ready, stopped: &stopped, cpuMillis: 1000, memoryBytes: 1 << 30})
	f.meter()
	entries := f.ledger(c)
	if len(entries) != 2 || !entries[0].end.Equal(change) || entries[0].version != "2026-09-09.a" || entries[1].version != "2026-09-10.a" {
		t.Fatalf("entries across the September change: %+v", entries)
	}
	// The earlier card's CPU rate is higher: $0.0551268 against $0.044.
	perSecond := func(e ledgerRow) float64 { return float64(e.cost) / e.end.Sub(e.start).Seconds() }
	if perSecond(entries[0]) <= perSecond(entries[1]) {
		t.Fatalf("rates did not change at the publication: %+v", entries)
	}
}

func TestMeteringFindsContainersThatLivedBetweenPasses(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	f.meter()
	// Ready and stopped since the last pass, so no pass saw it live.
	ready := time.Now().UTC().Truncate(time.Microsecond).Add(-30 * time.Second)
	stopped := ready.Add(12 * time.Second)
	c := f.container(containerSpec{workspace: ws, host: f.host(time.Now()), release: &rel.id, ready: ready, stopped: &stopped, cpuMillis: 125, memoryBytes: 128 << 20})
	f.meter()
	entries := f.ledger(c)
	if len(entries) == 0 || !entries[0].start.Equal(ready) || !entries[len(entries)-1].end.Equal(stopped) {
		t.Fatalf("short-lived container entries %+v", entries)
	}
}

// A build reserves 8 GiB to place it and bills 2 GiB of memory.
func TestMeteringBillsABuildTwoGiBWhileItReservesEight(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	build := f.imageBuild(ws)
	base := time.Now().UTC().Truncate(time.Hour).Add(-2 * time.Hour)
	ready, stopped := base.Add(3*time.Minute), base.Add(41*time.Minute)
	c := f.container(containerSpec{workspace: ws, host: f.host(time.Now()), build: &build, ready: ready, stopped: &stopped, cpuMillis: 4000, memoryBytes: 8 << 30})
	f.meter()
	var total int64
	for _, e := range f.ledger(c) {
		total += e.cost
	}
	billed := Shape{Owner: OwnerPlatformFleet, Class: ClassAuto, CPUMillis: 4000, MemoryBytes: 2 << 30}
	if want := f.expected(ready, billed, stopped.Sub(ready)); total == 0 || abs(total-want) > 3 {
		t.Fatalf("build billed %d, want %d, the cost of 4 CPUs and 2 GiB", total, want)
	}
}

// A container a replacement took over from bills until the first of its
// replacements was ready, or its own stop if that came first, and the
// replacement from then on, so the two never bill the same instant.
func TestMeteringEndsAReplacedContainerWhenItsReplacementIsReady(t *testing.T) {
	t.Parallel()
	base := time.Now().UTC().Truncate(time.Hour).Add(-3 * time.Hour)
	ready, takeover, stop := base.Add(5*time.Minute), base.Add(40*time.Minute), base.Add(50*time.Minute)
	for _, c := range []struct {
		name string
		// replacements are when each became ready, nil for never; stopped
		// is when the replaced container stopped, nil while it drains.
		replacements []*time.Time
		stopped      *time.Time
		end          time.Time
	}{
		{"replacement ready while the old one drains", []*time.Time{&takeover}, nil, takeover},
		{"replacement never ready", []*time.Time{nil}, &stop, stop},
		{"first replacement never ready, second ready", []*time.Time{nil, &takeover}, &stop, takeover},
		{"old one stops before its replacement is ready", []*time.Time{new(stop.Add(5 * time.Minute))}, &stop, stop},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			f := newFixture(t)
			ws := f.workspace(f.user())
			rel := f.release(ws)
			host := f.host(time.Now())
			old := f.container(containerSpec{workspace: ws, host: host, release: &rel.id, ready: ready, stopped: c.stopped, cpuMillis: 1000, memoryBytes: 1 << 30})
			var billed []uuid.UUID
			for _, at := range c.replacements {
				var id uuid.UUID
				err := f.pool.QueryRow(t.Context(), `
insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at, replaces)
values ($1, $2, case when $4::timestamptz is null then 'stopped' else 'ready' end, $3, 1, 1000, 1 << 30, $4, $4, $5) returning id`,
					ws, rel.id, host, at, old).Scan(&id)
				if err != nil {
					t.Fatal(err)
				}
				if at != nil {
					billed = append(billed, id)
				}
			}
			f.meter()
			entries := f.ledger(old)
			if len(entries) == 0 || !entries[0].start.Equal(ready) || !entries[len(entries)-1].end.Equal(c.end) {
				t.Fatalf("replaced container billed %+v, want [%s, %s)", entries, ready, c.end)
			}
			for _, id := range billed {
				next := f.ledger(id)
				if len(next) == 0 || next[0].start.Before(entries[len(entries)-1].end) {
					t.Fatalf("replacement billed from %+v while the replaced container billed to %s", next, entries[len(entries)-1].end)
				}
			}
		})
	}
}

func abs(n int64) int64 {
	if n < 0 {
		return -n
	}
	return n
}

// An attempt that ends by preemption, as GPU work's eviction or a Spot
// reclaim ends it, bills nothing; the attempts that completed on the same
// container bill, overlapping it or not. While attempts run no entry passes
// the start of the oldest, since preemption may yet void it. A period the
// voided time splits bills its measured use over the time it was read.
func TestMeteringChargesNothingForAPreemptedAttempt(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	ws := f.workspace(f.user())
	rel := f.release(ws)
	base := time.Now().UTC().Truncate(time.Hour).Add(-3 * time.Hour)
	at := func(minutes int) time.Time { return base.Add(time.Duration(minutes) * time.Minute) }
	c := f.container(containerSpec{workspace: ws, host: f.host(time.Now()), release: &rel.id, ready: at(5), cpuMillis: 1000, memoryBytes: 1 << 30})
	// attempt runs from start, finishing as state at end unless it is still
	// running.
	attempt := func(start time.Time, state string, end *time.Time) uuid.UUID {
		var id uuid.UUID
		if err := f.pool.QueryRow(t.Context(), `
with task as (insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts, started_at)
              values ($1, $2, $3, 'running', 1, 1, $5) returning id)
insert into attempts (task_id, number, container_id, state, started_at, finished_at, deadline_at)
select id, 1, $4, $6, $5, $7, now() + interval '1 hour' from task returning id`,
			ws, rel.workload, rel.id, c, start, state, end).Scan(&id); err != nil {
			t.Fatal(err)
		}
		return id
	}
	attempt(at(10), "succeeded", new(at(20)))
	overlapping := attempt(at(35), "running", nil)
	lost := attempt(at(40), "running", nil)

	f.meter()
	if got := f.ledger(c); len(got) != 2 || !got[1].end.Equal(at(30)) {
		t.Fatalf("while attempts run, entries %+v; want them to end at %s, the quarter-hour before the oldest", got, at(30))
	}
	f.exec("update attempts set state = 'succeeded', finished_at = $2 where id = $1", overlapping, at(45))
	attempt(at(50), "succeeded", new(at(55)))
	// Two cores through minutes 45 to 54, which the voided time splits.
	f.exec(`insert into container_metric_minutes (container_id, minute, samples, interval_ms, cpu_usage_usec, memory_rss_bytes,
            memory_swap_bytes, network_rx_bytes, network_tx_bytes, disk_read_bytes, disk_write_bytes)
select $1, $2::timestamptz + interval '1 minute' * g, 12, 60000, 120000000, 0, 0, 0, 0, 0, 0
from generate_series(0, 9) g`, c, at(45))
	f.exec("update attempts set state = 'lost', preempted = true, finished_at = now() where id = $1", lost)
	f.exec("update containers set state = 'stopped', stop_reason = 'stopped', stopped_at = now() where id = $1", c)
	f.meter()
	got := f.ledger(c)
	if n := len(got); n != 4 || !got[0].start.Equal(at(5)) || !got[2].end.Equal(at(45)) || !got[3].start.Equal(at(50)) || !got[3].end.Equal(at(55)) {
		t.Fatalf("entries %+v; want [%s, %s) up to the preempted attempt and on while the overlapping one ran, then [%s, %s)",
			got, at(5), at(45), at(50), at(55))
	}
	var millis int64
	if err := f.pool.QueryRow(t.Context(), "select cpu_millis from ledger_entries where source_id = $1 and started_at = $2", c, at(50)).Scan(&millis); err != nil {
		t.Fatal(err)
	}
	if millis != 2000 {
		t.Errorf("the entry after the voided time bills %d millicores, want the 2000 measured", millis)
	}
}
