package execution

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// FunctionRelease returns the release of a function's version.
func (e *Execution) FunctionRelease(ctx context.Context, workspace identity.WorkspaceID, app, function string, version int) (uuid.UUID, error) {
	id, err := e.queries.FunctionReleaseByVersion(ctx, FunctionReleaseByVersionParams{
		WorkspaceID: uuid.UUID(workspace), AppName: app, Name: function, Version: int32(version), //nolint:gosec // versions fit
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, ErrNotFound
	}
	if err != nil {
		return uuid.Nil, fmt.Errorf("read function version: %w", err)
	}
	return id, nil
}

// SubmitRelease admits every input or none against one release of a
// function rather than its active one: a pinned version, a release by id or
// a preview. A deployed version admits while its function is active; a
// preview while it runs.
func (e *Execution) SubmitRelease(ctx context.Context, workspace identity.WorkspaceID, release uuid.UUID, inputs []Payload) ([]Task, error) {
	encodings := make([]string, len(inputs))
	data := make([][]byte, len(inputs))
	for n, input := range inputs {
		if len(input.Data) > MaxPayloadBytes {
			return nil, ErrPayloadTooLarge
		}
		encodings[n] = string(input.Encoding)
		data[n] = input.Data
	}
	var tasks []Task
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		fn, err := q.LockReleaseForSubmit(ctx, LockReleaseForSubmitParams{WorkspaceID: uuid.UUID(workspace), ReleaseID: release})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock release: %w", err)
		}
		if fn.Kind != "function" {
			return ErrNotFound
		}
		accepting := fn.DesiredState == "active" && apitypes.AppState(fn.AppState) == apitypes.AppStateActive
		if fn.Version < 0 {
			accepting = fn.PreviewLive
		}
		if !accepting {
			return ErrNotAccepting
		}
		var spec apitypes.FunctionSpec
		if err := json.Unmarshal(fn.Spec, &spec); err != nil {
			return fmt.Errorf("decode release spec: %w", err)
		}
		limit := 0
		if spec.MaxPendingTasks != nil {
			limit = *spec.MaxPendingTasks
		}
		queued, err := q.CountQueuedTasks(ctx, fn.WorkloadID)
		if err != nil {
			return fmt.Errorf("count queued tasks: %w", err)
		}
		if int(queued)+len(inputs) > limit {
			return &TooManyPendingError{Limit: limit, Queued: int(queued), Submitted: len(inputs)}
		}
		rows, err := q.InsertTasks(ctx, InsertTasksParams{
			Encodings:   encodings,
			Data:        data,
			WorkspaceID: uuid.UUID(workspace),
			WorkloadID:  fn.WorkloadID,
			ReleaseID:   fn.ReleaseID,
			MaxAttempts: int32(RetryPolicyOf(spec).MaxAttempts), //nolint:gosec // The schema caps max_attempts at 100.
		})
		if err != nil {
			return fmt.Errorf("insert tasks: %w", err)
		}
		tasks = make([]Task, len(rows))
		for n, row := range rows {
			tasks[n] = Task{
				ID: TaskID(row.ID), App: fn.AppName, Function: fn.Name, Release: fn.ReleaseID,
				Status: TaskQueued, CreatedAt: row.CreatedAt,
			}
		}
		if err := database.Notify(ctx, tx, database.ChannelExecution, fn.ReleaseID.String()); err != nil {
			return err
		}
		return database.Notify(ctx, tx, database.ChannelClaim, fn.ReleaseID.String())
	})
	if err != nil {
		return nil, fmt.Errorf("submit to release %s: %w", release, err)
	}
	return tasks, nil
}
