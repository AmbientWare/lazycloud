package execution

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/compute"
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
	// ErrInvalidSubmit marks a submit the schema cannot reject.
	ErrInvalidSubmit = errors.New("invalid submit")
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

// UnknownTaskError rejects a submit that names a parent or upstream task the
// workspace does not have.
type UnknownTaskError struct {
	Role string
	Task TaskID
}

func (e *UnknownTaskError) Error() string {
	return fmt.Sprintf("%s task %s is not in the workspace", e.Role, e.Task)
}

// TaskInput is one task's arguments and the upstream tasks whose results
// the arguments refer to.
type TaskInput struct {
	Payload
	DependsOn []TaskID
}

// SubmitRequest admits one task per input.
type SubmitRequest struct {
	Workspace identity.WorkspaceID
	App       string
	Function  string
	// Release targets a release of the function instead of its active one.
	Release *uuid.UUID
	// Parent is the running task that spawns these. The root of their call
	// graph is read from the parent's row.
	Parent *TaskID
	// ScheduledFor is the cron occurrence admitting the task. One task
	// exists per occurrence.
	ScheduledFor *time.Time
	Inputs       []TaskInput
}

// Submit admits every input or none. The workload lock serializes submits of
// one function so max_pending_tasks counts exactly; the tasks and their
// inputs insert in one statement, pinned to the target release. A task whose
// upstream tasks have not all succeeded waits until they do; one whose
// upstream already failed fails at once. Planning and waiting claims wake
// when the transaction commits.
func (e *Execution) Submit(ctx context.Context, req SubmitRequest) ([]Task, error) {
	var tasks []Task
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		var err error
		tasks, err = e.SubmitInTx(ctx, tx, req)
		return err
	})
	if err != nil {
		return nil, fmt.Errorf("submit: %w", err)
	}
	return tasks, nil
}

// SubmitInTx admits req inside tx, so a caller can commit the admission with
// its own facts, such as a schedule's next occurrence.
func (e *Execution) SubmitInTx(ctx context.Context, tx pgx.Tx, req SubmitRequest) ([]Task, error) {
	encodings := make([]string, len(req.Inputs))
	data := make([][]byte, len(req.Inputs))
	var upstream []uuid.UUID
	for n, input := range req.Inputs {
		if len(input.Data) > MaxPayloadBytes {
			return nil, ErrPayloadTooLarge
		}
		if len(input.DependsOn) > MaxDependencies {
			return nil, fmt.Errorf("%w: an input names more than %d upstream tasks", ErrInvalidSubmit, MaxDependencies)
		}
		encodings[n] = string(input.Encoding)
		data[n] = input.Data
		for _, dep := range input.DependsOn {
			upstream = append(upstream, uuid.UUID(dep))
		}
	}
	slices.SortFunc(upstream, compareUUID)
	upstream = slices.Compact(upstream)

	var tasks []Task
	err := func() error {
		q := e.queries.WithTx(tx)
		fn, err := q.LockFunctionForSubmit(ctx, LockFunctionForSubmitParams{
			WorkspaceID: uuid.UUID(req.Workspace), AppName: req.App, Name: req.Function, ReleaseID: req.Release,
		})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock function: %w", err)
		}
		if !accepting(fn, req.Release != nil) {
			return ErrNotAccepting
		}
		var spec apitypes.WorkloadSpec
		if err := json.Unmarshal(fn.Spec, &spec); err != nil {
			return fmt.Errorf("decode release spec: %w", err)
		}
		// A run pinned to a machine fails at once while the machine cannot
		// take it; deployed calls wait for the machine.
		if machine := compute.PinnedMachine(spec); req.Release != nil && machine != "" {
			if err := compute.CheckMachineRuns(ctx, tx, uuid.UUID(req.Workspace), machine); err != nil {
				return err
			}
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
		live, err := q.CountLiveReleaseContainers(ctx, fn.ReleaseID)
		if err != nil {
			return fmt.Errorf("count live containers: %w", err)
		}
		if _, err := billing.Admit(ctx, tx, billing.Request{Workspace: uuid.UUID(req.Workspace), Cold: live == 0}); err != nil {
			return err
		}

		var parent, root *uuid.UUID
		if req.Parent != nil {
			row, err := q.ParentTask(ctx, ParentTaskParams{ID: uuid.UUID(*req.Parent), WorkspaceID: uuid.UUID(req.Workspace)})
			if errors.Is(err, pgx.ErrNoRows) {
				return &UnknownTaskError{Role: "parent", Task: *req.Parent}
			}
			if err != nil {
				return fmt.Errorf("read parent task: %w", err)
			}
			parent, root = &row.ID, row.RootTaskID
			if root == nil {
				root = &row.ID
			}
		}

		statuses, err := e.lockUpstream(ctx, q, req.Workspace, upstream)
		if err != nil {
			return err
		}
		unmet := make([]int32, len(req.Inputs))
		var doomed, tooLarge []int
		for n, input := range req.Inputs {
			failed := false
			size := int64(len(input.Data))
			for _, dep := range uniqueTasks(input.DependsOn) {
				upstream := statuses[uuid.UUID(dep)]
				switch upstream.status {
				case TaskQueued, TaskRunning:
					unmet[n]++
				case TaskFailed, TaskCancelled:
					failed = true
				case TaskSucceeded:
					size += upstream.resultBytes
				}
			}
			switch {
			case failed:
				doomed = append(doomed, n)
			case unmet[n] == 0 && size > MaxDependentInputBytes:
				// Every upstream already succeeded, so no later
				// resolution checks the size.
				tooLarge = append(tooLarge, n)
			}
		}

		ids := make([]uuid.UUID, len(req.Inputs))
		for n := range ids {
			if ids[n], err = uuid.NewV7(); err != nil {
				return fmt.Errorf("task id: %w", err)
			}
		}
		rows, err := insertSubmitted(ctx, tx, InsertTasksParams{
			Ids:          ids,
			Unmet:        unmet,
			WorkspaceID:  uuid.UUID(req.Workspace),
			WorkloadID:   fn.ID,
			ReleaseID:    fn.ReleaseID,
			MaxAttempts:  int32(RetryPolicyOf(spec).MaxAttempts), //nolint:gosec // The schema caps max_attempts at 100.
			ParentTaskID: parent,
			RootTaskID:   root,
			ScheduledFor: req.ScheduledFor,
			Traceparent:  traceparent(ctx),
		}, encodings, data, req.Inputs)
		if err != nil {
			return err
		}
		tasks = make([]Task, len(rows))
		for n, row := range rows {
			tasks[n] = Task{
				ID: TaskID(row.ID), App: fn.AppName, Function: fn.Name, Release: fn.ReleaseID, Version: versionOf(fn.Version),
				Status: TaskQueued, MaxAttempts: RetryPolicyOf(spec).MaxAttempts, Parent: taskIDPtr(parent), Root: TaskID(row.ID),
				ScheduledFor: req.ScheduledFor, CreatedAt: row.CreatedAt,
			}
			if root != nil {
				tasks[n].Root = TaskID(*root)
			}
		}
		failNew := func(indexes []int, failure Failure) error {
			if len(indexes) == 0 {
				return nil
			}
			ids := make([]uuid.UUID, len(indexes))
			releases := make([]uuid.UUID, len(indexes))
			for n, i := range indexes {
				ids[n], releases[n] = rows[i].ID, fn.ReleaseID
				tasks[i].Status = TaskFailed
				tasks[i].Failure = &failure
			}
			// New tasks have no dependents yet.
			return e.failQueued(ctx, tx, ids, releases, failure, false)
		}
		if err := failNew(doomed, Failure{Kind: FailureDependencyFailed, Message: "an upstream task failed or was cancelled"}); err != nil {
			return err
		}
		if err := failNew(tooLarge, dependenciesTooLarge()); err != nil {
			return err
		}
		if err := database.Notify(ctx, tx, database.ChannelExecution, fn.ReleaseID.String()); err != nil {
			return err
		}
		if err := database.Notify(ctx, tx, database.ChannelClaim, fn.ReleaseID.String()); err != nil {
			return err
		}
		return nil
	}()
	if err != nil {
		return nil, err
	}
	return tasks, nil
}

// accepting reports whether the function admits tasks for the release:
// the active release while the workload and app are active, a working-tree
// release while the app is active.
func accepting(fn LockFunctionForSubmitRow, explicit bool) bool {
	// A working-tree run and a preview, which `lazycloud serve` runs, are
	// the caller's own code, so pausing the app or stopping the workload
	// does not refuse them; they stop only what was deployed.
	if explicit && fn.Version == nil {
		return true
	}
	if explicit && *fn.Version < 0 {
		return fn.PreviewLive
	}
	return apitypes.AppState(fn.AppState) == apitypes.AppStateActive && fn.DesiredState == "active"
}

// lockUpstream holds the upstream tasks FOR SHARE in id order and returns
// their statuses. Tasks lock in id order everywhere, which follows creation
// order, so an upstream always locks before its dependents.
func (e *Execution) lockUpstream(ctx context.Context, q *Queries, workspace identity.WorkspaceID, upstream []uuid.UUID) (map[uuid.UUID]upstreamState, error) {
	if len(upstream) == 0 {
		return nil, nil
	}
	rows, err := q.LockUpstreamTasks(ctx, LockUpstreamTasksParams{Ids: upstream, WorkspaceID: uuid.UUID(workspace)})
	if err != nil {
		return nil, fmt.Errorf("lock upstream tasks: %w", err)
	}
	statuses := make(map[uuid.UUID]upstreamState, len(rows))
	for _, row := range rows {
		statuses[row.ID] = upstreamState{status: TaskStatus(row.Status), resultBytes: row.ResultBytes}
	}
	for _, id := range upstream {
		if _, ok := statuses[id]; !ok {
			return nil, &UnknownTaskError{Role: "upstream", Task: TaskID(id)}
		}
	}
	return statuses, nil
}

type upstreamState struct {
	status      TaskStatus
	resultBytes int64
}

func uniqueTasks(ids []TaskID) []TaskID {
	seen := make(map[TaskID]bool, len(ids))
	out := ids[:0:0]
	for _, id := range ids {
		if !seen[id] {
			seen[id] = true
			out = append(out, id)
		}
	}
	return out
}

// insertSubmitted inserts the tasks of p, their inputs and the dependency
// edges the inputs name in one round trip, and returns the tasks in input
// order. The submit picks the task ids, so no statement needs another's
// result; the batch runs them in order, so inputs and edges find their tasks.
func insertSubmitted(ctx context.Context, tx pgx.Tx, p InsertTasksParams, encodings []string, data [][]byte, inputs []TaskInput) ([]InsertTasksRow, error) {
	var inserted []InsertTasksRow
	batch := &pgx.Batch{}
	batch.Queue(insertTasks, p.Ids, p.WorkspaceID, p.WorkloadID, p.ReleaseID, p.MaxAttempts, p.ParentTaskID,
		p.RootTaskID, p.Unmet, p.ScheduledFor, p.Traceparent).Query(func(rows pgx.Rows) error {
		var err error
		if inserted, err = pgx.CollectRows(rows, pgx.RowToStructByPos[InsertTasksRow]); err != nil {
			return fmt.Errorf("read inserted tasks: %w", err)
		}
		return nil
	})
	batch.Queue(insertTaskInputs, p.Ids, encodings, data)
	var tasks, upstream []uuid.UUID
	for n, input := range inputs {
		seen := map[TaskID]bool{}
		for _, dep := range input.DependsOn {
			if seen[dep] {
				continue
			}
			seen[dep] = true
			tasks = append(tasks, p.Ids[n])
			upstream = append(upstream, uuid.UUID(dep))
		}
	}
	if len(tasks) > 0 {
		batch.Queue(insertDependencies, tasks, upstream)
	}
	if err := tx.SendBatch(ctx, batch).Close(); err != nil {
		return nil, fmt.Errorf("insert tasks: %w", err)
	}
	created := make(map[uuid.UUID]time.Time, len(inserted))
	for _, row := range inserted {
		created[row.ID] = row.CreatedAt
	}
	rows := make([]InsertTasksRow, len(p.Ids))
	for n, id := range p.Ids {
		rows[n] = InsertTasksRow{ID: id, CreatedAt: created[id]}
	}
	return rows, nil
}

func compareUUID(a, b uuid.UUID) int { return bytes.Compare(a[:], b[:]) }

func taskIDPtr(id *uuid.UUID) *TaskID {
	if id == nil {
		return nil
	}
	t := TaskID(*id)
	return &t
}

func versionOf(v *int32) *int {
	if v == nil {
		return nil
	}
	n := int(*v)
	return &n
}
