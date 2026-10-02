package billing

import (
	"testing"
	"time"
)

func TestMeteringBillsReadyToStoppedOnTheGrid(t *testing.T) {
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
	// 2 cores at $0.022/h and 4 GiB at $0.0075/h for 29m40s.
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
	// The earlier card's CPU rate is higher: $0.0551268 against $0.022.
	perSecond := func(e ledgerRow) float64 { return float64(e.cost) / e.end.Sub(e.start).Seconds() }
	if perSecond(entries[0]) <= perSecond(entries[1]) {
		t.Fatalf("rates did not change at the publication: %+v", entries)
	}
}

func TestMeteringFindsContainersThatLivedBetweenPasses(t *testing.T) {
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

func abs(n int64) int64 {
	if n < 0 {
		return -n
	}
	return n
}
