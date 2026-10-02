package storage

import (
	"context"
	"fmt"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// DeleteWorkspaceObjects deletes up to prefixChunk objects under the
// workspace's prefix, in batched requests, and aborts its multipart uploads.
// It reports whether the prefix is now empty; the caller repeats until it
// is, then removes the workspace's rows. Bounding each call keeps a pass
// short however many objects a workspace holds.
func (s *Storage) DeleteWorkspaceObjects(ctx context.Context, workspace identity.WorkspaceID) (bool, error) {
	empty, err := s.deletePrefixChunk(ctx, s.bucket, fmt.Sprintf("workspaces/%s/", workspace))
	if err != nil {
		return false, fmt.Errorf("delete workspace objects: %w", err)
	}
	return empty, nil
}
