package storage_test

import (
	"fmt"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
	. "github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// A workspace with more objects than one call deletes reports more work, so
// a deletion pass stays bounded, and the next call finishes the prefix.
func TestDeleteWorkspaceObjectsIsBoundedPerCall(t *testing.T) {
	ctx := t.Context()
	cfg := storagetest.Config(t)
	s := NewStorage(dbtest.New(t), cfg)
	ws := identity.WorkspaceID(uuid.New())
	prefix := fmt.Sprintf("workspaces/%s/", ws)
	putObjects(t, cfg.Bucket, prefix, 1001)

	empty, err := s.DeleteWorkspaceObjects(ctx, ws)
	if err != nil || empty {
		t.Fatalf("first call: empty %v err %v, want more objects left", empty, err)
	}
	if left := countObjects(t, cfg.Bucket, prefix); left != 1 {
		t.Fatalf("%d objects left after the first call, want 1", left)
	}
	if empty, err = s.DeleteWorkspaceObjects(ctx, ws); err != nil || !empty {
		t.Fatalf("second call: empty %v err %v, want empty", empty, err)
	}
	if left := countObjects(t, cfg.Bucket, prefix); left != 0 {
		t.Fatalf("%d objects left", left)
	}
}
