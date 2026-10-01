package execution

import (
	"context"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// unfundedBatch bounds the containers one StopUnfunded call stops; billing
// repeats while the account stays unfunded.
const unfundedBatch = 100

// StopUnfunded stops the workspace's function containers because its billing
// account may not run them: each drains, its running attempts end as lost
// and retry by policy, and planning starts no container until the account
// can pay. Image builds finish. It returns how many containers it stopped.
func (e *Execution) StopUnfunded(ctx context.Context, workspace identity.WorkspaceID, reason string) (int, error) {
	ids, err := e.queries.LiveFunctionContainers(ctx, LiveFunctionContainersParams{
		WorkspaceID: uuid.UUID(workspace), RowLimit: unfundedBatch,
	})
	if err != nil {
		return 0, fmt.Errorf("list live containers: %w", err)
	}
	cause := stopCause{exit: reason, lost: "the container was stopped: " + reason}
	for _, id := range ids {
		err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
			return e.stopContainer(ctx, tx, workspace, ContainerID(id), cause)
		})
		if err != nil {
			return 0, fmt.Errorf("stop unfunded container %s: %w", id, err)
		}
	}
	return len(ids), nil
}
