package storage

import (
	"context"
	"fmt"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// unfundedArtifactBatch bounds the artifacts one DeleteUnfunded call
// removes; billing repeats while the period stays expired.
const unfundedArtifactBatch = 1000

// DeleteUnfunded deletes the workspace's stored data after its owner's
// account stayed without credit through the retention period: volumes and
// disks no container uses are marked deleting for the sweep, and artifacts
// go now. It returns how many items it deleted.
func (s *Storage) DeleteUnfunded(ctx context.Context, workspace identity.WorkspaceID) (int, error) {
	volumes, err := s.queries.MarkWorkspaceVolumesDeleting(ctx, uuid.UUID(workspace))
	if err != nil {
		return 0, fmt.Errorf("delete volumes: %w", err)
	}
	disks, err := s.queries.MarkWorkspaceDisksDeleting(ctx, uuid.UUID(workspace))
	if err != nil {
		return 0, fmt.Errorf("delete disks: %w", err)
	}
	ids, err := s.queries.WorkspaceArtifacts(ctx, WorkspaceArtifactsParams{WorkspaceID: uuid.UUID(workspace), RowLimit: unfundedArtifactBatch})
	if err != nil {
		return 0, fmt.Errorf("list artifacts: %w", err)
	}
	deleted, err := s.DeleteArtifacts(ctx, workspace, ids)
	if err != nil {
		return 0, err
	}
	return int(volumes+disks) + len(deleted), nil
}
