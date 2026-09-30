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

var (
	// ErrNotFound means the function or task does not exist in the
	// workspace.
	ErrNotFound = errors.New("not found")
	// ErrNotAccepting means the function is stopped or its app paused, so
	// it admits no tasks.
	ErrNotAccepting = errors.New("function is not accepting tasks")
	// ErrPayloadTooLarge means an input or result exceeds MaxPayloadBytes.
	ErrPayloadTooLarge = errors.New("payload exceeds 16 MiB")
)

// TooManyPendingError rejects a submit that would queue more tasks than the
// function's max_pending_tasks.
type TooManyPendingError struct {
	Limit     int
	Queued    int
	Submitted int
}

func (e *TooManyPendingError) Error() string {
	return fmt.Sprintf("%d queued and %d submitted tasks exceed max_pending_tasks %d", e.Queued, e.Submitted, e.Limit)
}

// SubmitRequest admits one task per input against a function's active
// release.
type SubmitRequest struct {
	Workspace identity.WorkspaceID
	App       string
	Function  string
	Inputs    []Payload
}

// Submit admits every input or none. The workload lock serializes submits of
// one function so max_pending_tasks counts exactly; the tasks and their
// inputs insert in one statement, pinned to the active release. Planning and
// waiting claims wake when the transaction commits.
func (e *Execution) Submit(ctx context.Context, req SubmitRequest) ([]Task, error) {
	encodings := make([]string, len(req.Inputs))
	data := make([][]byte, len(req.Inputs))
	for n, input := range req.Inputs {
		if len(input.Data) > MaxPayloadBytes {
			return nil, ErrPayloadTooLarge
		}
		encodings[n] = string(input.Encoding)
		data[n] = input.Data
	}

	var tasks []Task
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		q := e.queries.WithTx(tx)
		fn, err := q.LockFunctionForSubmit(ctx, LockFunctionForSubmitParams{
			WorkspaceID: uuid.UUID(req.Workspace), AppName: req.App, Name: req.Function,
		})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock function: %w", err)
		}
		if fn.DesiredState != "active" || apitypes.AppState(fn.AppState) != apitypes.AppStateActive {
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
		queued, err := q.CountQueuedTasks(ctx, fn.ID)
		if err != nil {
			return fmt.Errorf("count queued tasks: %w", err)
		}
		if int(queued)+len(req.Inputs) > limit {
			return &TooManyPendingError{Limit: limit, Queued: int(queued), Submitted: len(req.Inputs)}
		}

		rows, err := q.InsertTasks(ctx, InsertTasksParams{
			Encodings:   encodings,
			Data:        data,
			WorkspaceID: uuid.UUID(req.Workspace),
			WorkloadID:  fn.ID,
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
		return nil, fmt.Errorf("submit: %w", err)
	}
	return tasks, nil
}
