package compute

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5"
)

// HostCapacity is a host's total and unreserved resources. Every container
// that is not stopped reserves its resources on its host.
type HostCapacity struct {
	Host            HostID
	CPUMillis       int64
	MemoryBytes     int64
	FreeCPUMillis   int64
	FreeMemoryBytes int64
}

// AvailableCapacity returns the online hosts that reported within
// LivenessTimeout with their unreserved resources, read in tx. The snapshot
// stays exact only while the caller serializes assignment.
func AvailableCapacity(ctx context.Context, tx pgx.Tx) ([]HostCapacity, error) {
	rows, err := New(tx).AvailableCapacity(ctx, LivenessTimeout.Seconds())
	if err != nil {
		return nil, fmt.Errorf("read host capacity: %w", err)
	}
	hosts := make([]HostCapacity, 0, len(rows))
	for _, row := range rows {
		hosts = append(hosts, HostCapacity{
			Host:            HostID(row.ID),
			CPUMillis:       row.CpuMillis,
			MemoryBytes:     row.MemoryBytes,
			FreeCPUMillis:   row.FreeCpuMillis,
			FreeMemoryBytes: row.FreeMemoryBytes,
		})
	}
	return hosts, nil
}
