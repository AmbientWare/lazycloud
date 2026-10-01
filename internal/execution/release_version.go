package execution

import (
	"context"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// FunctionRelease returns the release of a function's deployed version.
func (e *Execution) FunctionRelease(ctx context.Context, workspace identity.WorkspaceID, app, function string, version int) (uuid.UUID, error) {
	v := int32(version) //nolint:gosec // versions fit
	id, err := e.queries.FunctionReleaseByVersion(ctx, FunctionReleaseByVersionParams{
		WorkspaceID: uuid.UUID(workspace), AppName: app, Name: function, Version: &v,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, ErrNotFound
	}
	if err != nil {
		return uuid.Nil, fmt.Errorf("read function version: %w", err)
	}
	return id, nil
}
