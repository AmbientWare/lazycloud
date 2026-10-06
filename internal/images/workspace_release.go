package images

import (
	"context"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// ReleaseWorkspace prepares the builds of a workspace that identity marked
// deleting: running builds fail and their containers stop, and finished
// builds pass to another workspace that resolved the same image. Repeating
// it is harmless; the deletion coordinator calls it until the workspace is
// gone.
func (i *Images) ReleaseWorkspace(ctx context.Context, workspace identity.WorkspaceID) error {
	running, err := i.queries.BuildingWorkspaceBuilds(ctx, uuid.UUID(workspace))
	if err != nil {
		return fmt.Errorf("list running builds: %w", err)
	}
	for _, build := range running {
		if err := i.failBuild(ctx, build.ID, build.ImageDigest, "the workspace that started the build was deleted"); err != nil {
			return err
		}
	}
	if err := i.queries.HandOffWorkspaceBuilds(ctx, uuid.UUID(workspace)); err != nil {
		return fmt.Errorf("hand off builds: %w", err)
	}
	return nil
}

// failBuild ends a running build with reason and stops its containers.
func (i *Images) failBuild(ctx context.Context, id uuid.UUID, digest []byte, reason string) error {
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		// Lock order: image, then build.
		if _, err := q.LockImage(ctx, digest); err != nil {
			return fmt.Errorf("lock image: %w", err)
		}
		build, err := q.LockBuild(ctx, id)
		if err != nil {
			return fmt.Errorf("lock build: %w", err)
		}
		if BuildStatus(build.State) != BuildBuilding {
			return nil
		}
		return i.failLocked(ctx, tx, id, reason, false)
	})
	if err != nil {
		return fmt.Errorf("fail build %s: %w", id, err)
	}
	return nil
}
