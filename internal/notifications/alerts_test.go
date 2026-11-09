package notifications

import (
	"log/slog"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
)

// Spend over a limit emails the administrators once per crossing: staying
// over sends nothing more, falling back under re-arms it, and so does
// changing the limit.
func TestSpendAlertsSendOncePerCrossing(t *testing.T) {
	t.Parallel()
	pool := dbtest.New(t)
	n := NewNotifications(pool, nil, slog.New(slog.DiscardHandler))
	limit := int64(10_000_000)
	if err := n.SetAdminAlerts(t.Context(), AdminAlerts{Email: "ops@example.com", HourlyLimitMicros: &limit}); err != nil {
		t.Fatal(err)
	}
	sent := func() int {
		t.Helper()
		var count int
		if err := pool.QueryRow(t.Context(), "select count(*) from email_outbox where recipient = 'ops@example.com'").Scan(&count); err != nil {
			t.Fatal(err)
		}
		return count
	}
	for _, step := range []struct {
		hourly int64
		want   int
	}{{9_000_000, 0}, {12_000_000, 1}, {15_000_000, 1}, {8_000_000, 1}, {11_000_000, 2}} {
		if err := n.AlertSpend(t.Context(), step.hourly, 0); err != nil {
			t.Fatal(err)
		}
		if got := sent(); got != step.want {
			t.Fatalf("after spend of %d micros an hour, %d alerts sent, want %d", step.hourly, got, step.want)
		}
	}
	lower := int64(5_000_000)
	if err := n.SetAdminAlerts(t.Context(), AdminAlerts{Email: "ops@example.com", HourlyLimitMicros: &lower}); err != nil {
		t.Fatal(err)
	}
	if err := n.AlertSpend(t.Context(), 11_000_000, 0); err != nil {
		t.Fatal(err)
	}
	if got := sent(); got != 3 {
		t.Fatalf("after lowering the limit under spend, %d alerts sent, want 3", got)
	}
}
