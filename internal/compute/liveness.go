package compute

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// LivenessTimeout is how long an online host may stay silent before it is
// lost. Placement ignores hosts silent for longer.
const LivenessTimeout = 30 * time.Second

// StaleHost is an online host whose last report is older than
// LivenessTimeout.
type StaleHost struct {
	ID         HostID
	LastSeenAt time.Time
}

// StaleHosts returns up to limit stale hosts ordered by last report, starting
// after the cursor host. Pass nil to start from the oldest.
func StaleHosts(ctx context.Context, db DBTX, after *StaleHost, limit int32) ([]StaleHost, error) {
	params := StaleHostsParams{TimeoutSeconds: LivenessTimeout.Seconds(), BatchSize: limit}
	if after != nil {
		params.AfterSeenAt, params.AfterID = after.LastSeenAt, uuid.UUID(after.ID)
	}
	rows, err := New(db).StaleHosts(ctx, params)
	if err != nil {
		return nil, fmt.Errorf("list stale hosts: %w", err)
	}
	hosts := make([]StaleHost, 0, len(rows))
	for _, row := range rows {
		// The query filters on last_seen_at, so it is never null here.
		hosts = append(hosts, StaleHost{ID: HostID(row.ID), LastSeenAt: *row.LastSeenAt})
	}
	return hosts, nil
}

// MarkLost moves a host that is still online and stale to lost in tx and
// reports whether it did. The caller stops the host's containers in the same
// transaction.
func MarkLost(ctx context.Context, tx pgx.Tx, host HostID) (bool, error) {
	n, err := New(tx).MarkHostLost(ctx, MarkHostLostParams{ID: uuid.UUID(host), TimeoutSeconds: LivenessTimeout.Seconds()})
	if err != nil {
		return false, fmt.Errorf("mark host %s lost: %w", host, err)
	}
	return n == 1, nil
}
