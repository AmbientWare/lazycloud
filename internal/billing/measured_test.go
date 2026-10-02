package billing

import (
	"testing"
	"time"
)

// CPU and memory bill at the greater of the reservation and measured use:
// a period that used more than it held bills what it used, one that used
// less, or reported nothing, bills the reservation. Recent periods read the
// unfolded samples and older ones the minutes, so nothing counts twice.
func TestMeteringBillsTheGreaterOfReservationAndMeasuredUse(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	base := time.Now().UTC().Truncate(time.Hour).Add(-3 * time.Hour)
	stopped := base.Add(45 * time.Minute)
	host := f.host(time.Now())
	reserved := Shape{Owner: OwnerPlatformFleet, Class: ClassAuto, CPUMillis: 500, MemoryBytes: 1 << 30}
	c := f.container(containerSpec{workspace: ws, host: host, release: &rel.id, ready: base, stopped: &stopped, cpuMillis: 500, memoryBytes: 1 << 30})

	// First period: two cores and 2 GiB, from the folded minutes.
	f.exec(`insert into container_metric_minutes (container_id, minute, samples, interval_ms, cpu_usage_usec, memory_rss_bytes,
            memory_swap_bytes, network_rx_bytes, network_tx_bytes, disk_read_bytes, disk_write_bytes)
select $1, $2::timestamptz + interval '1 minute' * g, 12, 60000, 120000000, 2147483648, 0, 0, 0, 0, 0
from generate_series(0, 14) g`, c, base)
	// Second period: a tenth of a core, under the reservation.
	f.exec(`insert into container_metric_minutes (container_id, minute, samples, interval_ms, cpu_usage_usec, memory_rss_bytes,
            memory_swap_bytes, network_rx_bytes, network_tx_bytes, disk_read_bytes, disk_write_bytes)
select $1, $2::timestamptz + interval '15 minutes' + interval '1 minute' * g, 12, 60000, 6000000, 104857600, 0, 0, 0, 0, 0
from generate_series(0, 14) g`, c, base)
	// The third reported nothing; rows past the rollup's mark are not read
	// from minutes, which only the samples cover there.
	f.exec("update container_metric_rollup set rolled_through = $1", base.Add(30*time.Minute))
	f.exec(`insert into container_metric_minutes (container_id, minute, samples, interval_ms, cpu_usage_usec, memory_rss_bytes,
            memory_swap_bytes, network_rx_bytes, network_tx_bytes, disk_read_bytes, disk_write_bytes)
values ($1, $2::timestamptz + interval '30 minutes', 12, 60000, 900000000, 8589934592, 0, 0, 0, 0, 0)`, c, base)

	f.meter()
	got := f.ledger(c)
	if len(got) != 3 {
		t.Fatalf("entries %+v, want three periods", got)
	}
	used := reserved
	used.CPUMillis, used.MemoryBytes = 2000, 2<<30
	for n, want := range []Shape{used, reserved, reserved} {
		if cost := f.expected(got[n].start, want, MeteringPeriod); got[n].cost != cost {
			t.Errorf("period %d costs %d, want %d for %+v", n, got[n].cost, cost, want)
		}
	}
	var millis, bytes int64
	if err := f.pool.QueryRow(t.Context(), "select cpu_millis, memory_bytes from ledger_entries where source_id = $1 order by started_at limit 1", c).Scan(&millis, &bytes); err != nil {
		t.Fatal(err)
	}
	if millis != 2000 || bytes != 2<<30 {
		t.Fatalf("the ledger records %d millicores and %d bytes, want what it billed", millis, bytes)
	}
}
