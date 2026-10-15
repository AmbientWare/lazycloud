package storage

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/imagefs/imagefsproto"
)

// volumeTraceAge is when a workload's volume trace is recorded again: its
// releases mostly read the same weights, and a deploy that reads others is
// caught within a day.
const volumeTraceAge = 24 * time.Hour

// VolumeTrace returns the volume objects the workload's containers read
// through their first task or request, empty when none is stored, and
// whether the next start should record them again. A trace stays with its
// workload, so no other workspace's host gets it.
func (s *Storage) VolumeTrace(ctx context.Context, workspace identity.WorkspaceID, workload uuid.UUID) ([]*imagefsproto.VolumeRead, bool, error) {
	row, err := s.queries.VolumeTrace(ctx, VolumeTraceParams{WorkloadID: workload, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, true, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("read volume trace: %w", err)
	}
	reads := make([]*imagefsproto.VolumeRead, len(row.VolumeIds))
	for n := range reads {
		reads[n] = &imagefsproto.VolumeRead{VolumeId: row.VolumeIds[n].String(), Path: row.Paths[n]}
	}
	return reads, time.Since(row.RecordedAt) >= volumeTraceAge, nil
}

// RecordVolumeTrace stores the volume objects a container of the workload
// read, keeping only those under volumes the container mounts in the
// workspace, unless a trace younger than volumeTraceAge is stored.
func (s *Storage) RecordVolumeTrace(ctx context.Context, workspace identity.WorkspaceID, workload, container uuid.UUID, reads []*imagefsproto.VolumeRead) error {
	if len(reads) > hostproto.MaxVolumeReads {
		reads = reads[:hostproto.MaxVolumeReads]
	}
	params := RecordVolumeTraceParams{
		WorkloadID: workload, WorkspaceID: uuid.UUID(workspace), ContainerID: container, MaxAgeSeconds: volumeTraceAge.Seconds(),
	}
	for _, r := range reads {
		volume, err := uuid.Parse(r.GetVolumeId())
		if err != nil {
			continue
		}
		params.VolumeIds = append(params.VolumeIds, volume)
		params.Paths = append(params.Paths, r.GetPath())
	}
	if err := s.queries.RecordVolumeTrace(ctx, params); err != nil {
		return fmt.Errorf("record volume trace: %w", err)
	}
	return nil
}
