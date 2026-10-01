package storage_test

import (
	"log/slog"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/billing"
)

func TestArtifactSummaryShowsTheMeteredCost(t *testing.T) {
	ctx := t.Context()
	f := newFixture(t, `{}`)
	saveArtifact(t, f, f.task(), "report.bin", "application/octet-stream", make([]byte, 1<<20))
	now := time.Now().UTC()
	since := time.Date(now.Year(), now.Month(), 1, 0, 0, 0, 0, time.UTC)
	// Stored three hours ago, so closed quarter-hours have been held.
	if _, err := f.pool.Exec(ctx, "update artifacts set stored_at = now() - interval '3 hours'"); err != nil {
		t.Fatal(err)
	}
	before, err := f.storage.ArtifactSummary(ctx, f.ws)
	if err != nil || before.AccruedNanos != 0 || !before.AccruedSince.Equal(since) || before.EstimatedMonthlyNanos <= 0 {
		t.Fatalf("summary before metering: %+v %v", before, err)
	}
	if _, err := billing.NewBilling(f.pool, billing.Config{}, slog.New(slog.DiscardHandler)).Meter(ctx); err != nil {
		t.Fatal(err)
	}
	var metered int64
	if err := f.pool.QueryRow(ctx, "select coalesce(sum(cost_nanos), 0)::bigint from ledger_entries where source_kind = 'artifacts' and started_at >= $1", since).Scan(&metered); err != nil {
		t.Fatal(err)
	}
	if now.Sub(since) > billing.MeteringPeriod && metered == 0 {
		t.Fatal("metering priced no artifact storage")
	}
	after, err := f.storage.ArtifactSummary(ctx, f.ws)
	if err != nil || after.AccruedNanos != metered || after.EstimatedMonthlyNanos != before.EstimatedMonthlyNanos {
		t.Fatalf("summary after metering %+v, want %d accrued: %v", after, metered, err)
	}
}
