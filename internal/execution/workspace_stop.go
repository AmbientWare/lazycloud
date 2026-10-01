package execution

import (
	"context"
	"fmt"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// stopWorkspaceBatch bounds the running tasks one StopWorkspace call
// cancels; the caller repeats until no containers remain.
const stopWorkspaceBatch = 100

// StopWorkspace winds down a workspace that identity marked deleting and
// returns how many of its containers are still live. Planning already
// treats its releases as stopping: it cancels queued tasks, stops pending
// containers and drains the rest. This cancels running tasks, so draining
// containers go idle and their hosts stop them, instead of finishing work
// nobody will read.
func (e *Execution) StopWorkspace(ctx context.Context, workspace identity.WorkspaceID) (int, error) {
	running, err := e.queries.RunningWorkspaceTasks(ctx, RunningWorkspaceTasksParams{
		WorkspaceID: uuid.UUID(workspace), RowLimit: stopWorkspaceBatch,
	})
	if err != nil {
		return 0, fmt.Errorf("list running tasks: %w", err)
	}
	for _, task := range running {
		if _, err := e.CancelTask(ctx, workspace, TaskID(task)); err != nil {
			return 0, err
		}
	}
	live, err := e.queries.LiveWorkspaceContainers(ctx, uuid.UUID(workspace))
	if err != nil {
		return 0, fmt.Errorf("count live containers: %w", err)
	}
	return int(live), nil
}
