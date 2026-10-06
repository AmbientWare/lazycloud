package images

import (
	"context"
	"fmt"

	"github.com/google/uuid"

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
