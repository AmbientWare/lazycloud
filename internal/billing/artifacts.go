package billing

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"
)

// ArtifactCharges is what a workspace's stored artifacts cost.
type ArtifactCharges struct {
	// AccruedNanos is the metered cost since Since, the start of the UTC
	// month. The open quarter-hour is metered when it closes.
	AccruedNanos int64
	Since        time.Time
	// MonthlyNanos is what storedBytes cost for a 30-day month at the rate
	// in force now.
	MonthlyNanos int64
}

// ArtifactCost reads the workspace's artifact charges this month from the
// ledger and prices storedBytes for a month. Artifacts are billed as volume
// storage.
func ArtifactCost(ctx context.Context, db DBTX, workspace uuid.UUID, storedBytes int64, now time.Time) (ArtifactCharges, error) {
	now = now.UTC()
	since := time.Date(now.Year(), now.Month(), 1, 0, 0, 0, 0, time.UTC)
	accrued, err := New(db).WorkspaceArtifactCost(ctx, WorkspaceArtifactCostParams{WorkspaceID: workspace, Since: since})
	if err != nil {
		return ArtifactCharges{}, fmt.Errorf("read artifact cost: %w", err)
	}
	card, err := newRates().cardAt(now)
	if err != nil {
		return ArtifactCharges{}, err
	}
	return ArtifactCharges{
		AccruedNanos: accrued, Since: since,
		MonthlyNanos: storageCost(card.Platform.VolumeGiBMonth, storedBytes, storageMonthSeconds*time.Second),
	}, nil
}
