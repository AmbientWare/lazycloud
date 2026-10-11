package storage

import (
	"context"
	"time"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// SetOrphanAge shortens how old an unowned object must be before the sweep
// removes it, so tests need not wait hours.
func SetOrphanAge(s *Storage, age time.Duration) { s.orphanAge = age }

// CreateWorkspaceBucket creates the platform workspace's bucket as its
// first use does and returns the bucket and region storage then uses.
func CreateWorkspaceBucket(ctx context.Context, s *Storage, workspace identity.WorkspaceID) (string, string, error) {
	store, err := s.createWorkspaceBucket(ctx, workspace, nil)
	return store.name, store.region, err
}
