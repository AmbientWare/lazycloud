package execution

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

var (
	// ErrTaskNotFinished means the task is still queued or running.
	ErrTaskNotFinished = errors.New("task has not finished")
	// ErrNoResult means the task finished without a result: it failed or
	// was cancelled.
	ErrNoResult = errors.New("task has no result")
	// ErrInvalidCursor means a page cursor did not come from this listing.
	ErrInvalidCursor = errors.New("invalid page cursor")
	// ErrInvalidFilter means a listing filter combination is unsupported.
	ErrInvalidFilter = errors.New("invalid filter")
)

// Task is a task as callers see it.
type Task struct {
	ID       TaskID
	App      string
	Function string
	Release  uuid.UUID
	// Version is the release's deployed version, nil for a working-tree
	// release.
	Version     *int
	Status      TaskStatus
	Attempts    int
	MaxAttempts int
	// Preemptions counts the attempts lost to preemption.
	Preemptions int
	// Parent is the task that spawned this one; Root is the root of its
	// call graph, the task itself when nothing spawned it.
	Parent *TaskID
	Root   TaskID
	// ScheduledFor is the cron occurrence that admitted the task.
	ScheduledFor *time.Time
	// Container ran the latest attempt.
	Container *ContainerID
	// NextAttemptAt is when a queued task that already ran is due again.
	NextAttemptAt *time.Time
	Pending       *PendingProgress
	CreatedAt     time.Time
	StartedAt     *time.Time
	FinishedAt    *time.Time
	Failure       *Failure
}

// GetTask reads a task in workspace. With wait above zero it holds until the
// task is terminal, wait passes or ctx ends, and returns the latest state.
func (e *Execution) GetTask(ctx context.Context, listener *database.Listener, workspace identity.WorkspaceID, id TaskID, wait time.Duration) (Task, error) {
	if wait <= 0 {
		return e.readTask(ctx, workspace, id)
	}
	// Subscribe before reading so a completion between the read and the wait
	// still wakes this call.
	wake, cancel := listener.Subscribe(database.ChannelTask, id.String())
	defer cancel()
	timer := time.NewTimer(wait)
	defer timer.Stop()
	for {
		task, err := e.readTask(ctx, workspace, id)
		if err != nil || task.Status.Terminal() {
			return task, err
		}
		select {
		case <-wake:
		case <-timer.C:
			return task, nil
		case <-ctx.Done():
			return task, nil
		}
	}
}

// FinishedTask is a finished task with its result when the result comes
// inline.
type FinishedTask struct {
	Task   Task
	Result *Payload
	// ResultOmitted means the task has a result that did not fit the inline
	// budget; TaskResult reads it.
	ResultOmitted bool
}

// TaskWait is what WaitTasks found among the requested tasks.
type TaskWait struct {
	Finished []FinishedTask
	// Missing are the requested ids the workspace has no task for.
	Missing []TaskID
}

const (
	// maxInlineResult bounds one result inlined by WaitTasks, as the API
	// encodes it.
	maxInlineResult = 256 << 10
	// maxInlineResults bounds all results inlined by one WaitTasks call.
	maxInlineResults = 4 << 20
)

// WaitTasks returns the finished tasks among ids and the ids the workspace
// has no task for, each in the order of ids. With wait above zero it holds
// until it finds either, wait passes or ctx ends.
func (e *Execution) WaitTasks(ctx context.Context, listener *database.Listener, workspace identity.WorkspaceID, ids []TaskID, wait time.Duration) (TaskWait, error) {
	keys := make([]uuid.UUID, len(ids))
	for n, id := range ids {
		keys[n] = uuid.UUID(id)
	}
	var wake <-chan struct{}
	if wait > 0 {
		// Subscribe before the first read so a finish between a read and
		// the wait still wakes this call.
		var cancel func()
		wake, cancel = listener.Subscribe(database.ChannelTaskFinished, uuidStrings(keys)...)
		defer cancel()
	}
	missing, err := e.queries.MissingTasks(ctx, MissingTasksParams{Ids: keys, WorkspaceID: uuid.UUID(workspace)})
	if err != nil {
		return TaskWait{}, fmt.Errorf("find missing tasks: %w", err)
	}
	result := TaskWait{Missing: make([]TaskID, len(missing))}
	for n, id := range missing {
		result.Missing[n] = TaskID(id)
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	for {
		result.Finished, err = e.finishedTasks(ctx, workspace, keys)
		if err != nil || len(result.Finished) > 0 || len(result.Missing) > 0 || wait <= 0 {
			return result, err
		}
		select {
		case <-wake:
		case <-timer.C:
			return result, nil
		case <-ctx.Done():
			return result, nil
		}
	}
}

func (e *Execution) finishedTasks(ctx context.Context, workspace identity.WorkspaceID, ids []uuid.UUID) ([]FinishedTask, error) {
	rows, err := e.queries.FinishedTasks(ctx, FinishedTasksParams{
		Ids: ids, WorkspaceID: uuid.UUID(workspace), ResultMax: maxInlineResult, TotalMax: maxInlineResults,
	})
	if err != nil {
		return nil, fmt.Errorf("read finished tasks: %w", err)
	}
	finished := make([]FinishedTask, len(rows))
	for n, row := range rows {
		task, err := taskFrom(TaskViewsRow{
			ID: row.ID, AppName: row.AppName, FunctionName: row.FunctionName, ReleaseID: row.ReleaseID,
			Version: row.Version, Status: row.Status, AttemptCount: row.AttemptCount, MaxAttempts: row.MaxAttempts,
			Preemptions: row.Preemptions, ParentTaskID: row.ParentTaskID, RootTaskID: row.RootTaskID, AvailableAt: row.AvailableAt,
			CreatedAt: row.CreatedAt, StartedAt: row.StartedAt, FinishedAt: row.FinishedAt,
			Failure: row.Failure, ScheduledFor: row.ScheduledFor, ContainerIds: row.ContainerIds,
		})
		if err != nil {
			return nil, err
		}
		finished[n].Task = task
		switch {
		case row.Encoding == nil:
		case row.Inline:
			finished[n].Result = &Payload{Encoding: Encoding(*row.Encoding), Data: row.Data, Display: row.Display}
		default:
			finished[n].ResultOmitted = true
		}
	}
	return finished, nil
}

func (e *Execution) readTask(ctx context.Context, workspace identity.WorkspaceID, id TaskID) (Task, error) {
	tasks, err := e.taskViews(ctx, workspace, []uuid.UUID{uuid.UUID(id)})
	if err != nil {
		return Task{}, err
	}
	if len(tasks) == 0 {
		return Task{}, ErrNotFound
	}
	return tasks[0], nil
}

// taskViews reads the workspace's tasks of ids, newest first, queued ones
// with their pending progress.
func (e *Execution) taskViews(ctx context.Context, workspace identity.WorkspaceID, ids []uuid.UUID) ([]Task, error) {
	rows, err := e.queries.TaskViews(ctx, TaskViewsParams{Ids: ids, WorkspaceID: uuid.UUID(workspace)})
	if err != nil {
		return nil, fmt.Errorf("read tasks: %w", err)
	}
	tasks := make([]Task, len(rows))
	for n, row := range rows {
		if tasks[n], err = taskFrom(row); err != nil {
			return nil, err
		}
	}
	if err := e.addPending(ctx, tasks); err != nil {
		return nil, err
	}
	return tasks, nil
}

func taskFrom(row TaskViewsRow) (Task, error) {
	task := Task{
		ID: TaskID(row.ID), App: row.AppName, Function: row.FunctionName, Release: row.ReleaseID,
		Version: versionOf(row.Version), Status: TaskStatus(row.Status),
		Attempts: int(row.AttemptCount), MaxAttempts: int(row.MaxAttempts), Preemptions: int(row.Preemptions),
		Parent: taskIDPtr(row.ParentTaskID), Root: TaskID(row.ID), ScheduledFor: row.ScheduledFor,
		CreatedAt: row.CreatedAt, StartedAt: row.StartedAt, FinishedAt: row.FinishedAt,
	}
	if row.RootTaskID != nil {
		task.Root = TaskID(*row.RootTaskID)
	}
	if len(row.ContainerIds) > 0 {
		c := ContainerID(row.ContainerIds[0])
		task.Container = &c
	}
	if task.Status == TaskQueued && task.Attempts > 0 {
		at := row.AvailableAt
		task.NextAttemptAt = &at
	}
	if row.Failure != nil {
		task.Failure = &Failure{}
		if err := json.Unmarshal(row.Failure, task.Failure); err != nil {
			return Task{}, fmt.Errorf("decode task failure: %w", err)
		}
	}
	return task, nil
}

// TaskFilter narrows ListTasks.
type TaskFilter struct {
	App *string
	// Function requires App.
	Function *string
	Status   *TaskStatus
	// RootOnly leaves out tasks spawned by other tasks.
	RootOnly bool
	// Search matches a task id prefix or part of the function name.
	Search *string
	// Version is a deployed version of Function, and requires it.
	Version *int
}

// TaskPage is one page of tasks, newest first.
type TaskPage struct {
	Tasks []Task
	Next  string
}

// ListTasks returns the workspace's tasks newest first, after the cursor.
// Queued tasks carry their pending progress.
func (e *Execution) ListTasks(ctx context.Context, workspace identity.WorkspaceID, filter TaskFilter, limit int, cursor string) (TaskPage, error) {
	before := uuid.Max
	if cursor != "" {
		id, err := uuid.Parse(cursor)
		if err != nil {
			return TaskPage{}, ErrInvalidCursor
		}
		before = id
	}
	if filter.Function != nil && filter.App == nil {
		return TaskPage{}, fmt.Errorf("%w: function needs app", ErrInvalidFilter)
	}
	if filter.Version != nil && filter.Function == nil {
		return TaskPage{}, fmt.Errorf("%w: version needs function", ErrInvalidFilter)
	}
	size := pageSize(limit)
	ids, err := e.taskIDs(ctx, workspace, filter, before, size+1)
	if err != nil {
		return TaskPage{}, fmt.Errorf("list tasks: %w", err)
	}
	page := TaskPage{Tasks: []Task{}}
	if len(ids) > int(size) {
		ids = ids[:size]
		page.Next = ids[len(ids)-1].String()
	}
	if len(ids) == 0 {
		return page, nil
	}
	if page.Tasks, err = e.taskViews(ctx, workspace, ids); err != nil {
		return TaskPage{}, err
	}
	return page, nil
}

// taskIDs reads the ids of a page of the filter's tasks below before,
// newest first, from one index range per workspace, workload or release,
// and per status and spawned flag when the filter names a status or roots
// only.
func (e *Execution) taskIDs(ctx context.Context, workspace identity.WorkspaceID, filter TaskFilter, before uuid.UUID, rows int32) ([]uuid.UUID, error) {
	byStatus := filter.Status != nil || filter.RootOnly
	var statuses []string
	for _, status := range taskStatuses() {
		if filter.Status == nil || status == *filter.Status {
			statuses = append(statuses, string(status))
		}
	}
	spawned := []bool{false, true}
	if filter.RootOnly {
		spawned = []bool{false}
	}
	search := ""
	if filter.Search != nil {
		search = strings.ToLower(*filter.Search)
	}
	if filter.App == nil && search == "" {
		if byStatus {
			return e.queries.WorkspaceTaskIDsByStatus(ctx, WorkspaceTaskIDsByStatusParams{
				Statuses: statuses, Spawned: spawned, WorkspaceID: uuid.UUID(workspace), Before: before, MaxRows: rows,
			})
		}
		return e.queries.WorkspaceTaskIDs(ctx, WorkspaceTaskIDsParams{WorkspaceID: uuid.UUID(workspace), Before: before, MaxRows: rows})
	}
	workloads, err := e.queries.ListingWorkloads(ctx, ListingWorkloadsParams{
		Version: int32Of(filter.Version), WorkspaceID: uuid.UUID(workspace), App: filter.App, Function: filter.Function,
	})
	if err != nil {
		return nil, fmt.Errorf("find workloads: %w", err)
	}
	// A search reads all of a workload whose name contains it, and otherwise
	// only the ids whose text starts with it.
	prefixLow, prefixHigh, idPrefix := idPrefixRange(search)
	var keys, lows, highs []uuid.UUID
	for _, w := range workloads {
		key := w.ID
		if filter.Version != nil {
			if w.ReleaseID == nil {
				continue
			}
			key = *w.ReleaseID
		}
		low, high := uuid.Nil, uuid.Max
		if !strings.Contains(strings.ToLower(w.Name), search) {
			if !idPrefix {
				continue
			}
			low, high = prefixLow, prefixHigh
		}
		keys, lows, highs = append(keys, key), append(lows, low), append(highs, high)
	}
	switch {
	case len(keys) == 0:
		return nil, nil
	case filter.Version != nil:
		return e.queries.ReleaseTaskIDs(ctx, ReleaseTaskIDsParams{
			ReleaseIds: keys, IDLows: lows, IDHighs: highs, Statuses: statuses, Spawned: spawned, Before: before, MaxRows: rows,
		})
	case byStatus:
		return e.queries.WorkloadTaskIDsByStatus(ctx, WorkloadTaskIDsByStatusParams{
			WorkloadIds: keys, IDLows: lows, IDHighs: highs, Statuses: statuses, Spawned: spawned, Before: before, MaxRows: rows,
		})
	default:
		return e.queries.WorkloadTaskIDs(ctx, WorkloadTaskIDsParams{
			WorkloadIds: keys, IDLows: lows, IDHighs: highs, Before: before, MaxRows: rows,
		})
	}
}

// idPrefixRange is the range of task ids whose text form starts with
// prefix, and false when no id's can.
func idPrefixRange(prefix string) (uuid.UUID, uuid.UUID, bool) {
	const zeros = "00000000-0000-0000-0000-000000000000"
	if len(prefix) > len(zeros) {
		return uuid.UUID{}, uuid.UUID{}, false
	}
	for i, c := range prefix {
		if (zeros[i] == '-') != (c == '-') || c != '-' && !strings.ContainsRune("0123456789abcdef", c) {
			return uuid.UUID{}, uuid.UUID{}, false
		}
	}
	rest := zeros[len(prefix):]
	low, errLow := uuid.Parse(prefix + rest)
	high, errHigh := uuid.Parse(prefix + strings.ReplaceAll(rest, "0", "f"))
	if errLow != nil || errHigh != nil {
		return uuid.UUID{}, uuid.UUID{}, false
	}
	return low, high, true
}

// maxPage bounds every page of a listing.
const maxPage = 1000

func pageSize(limit int) int32 {
	if limit <= 0 {
		return 100
	}
	return int32(min(limit, maxPage)) //nolint:gosec // Bounded by maxPage.
}

// TaskResult returns the value a succeeded task returned.
func (e *Execution) TaskResult(ctx context.Context, workspace identity.WorkspaceID, id TaskID) (Payload, error) {
	row, err := e.queries.TaskResult(ctx, TaskResultParams{ID: uuid.UUID(id), WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return Payload{}, ErrNotFound
	}
	if err != nil {
		return Payload{}, fmt.Errorf("read task result: %w", err)
	}
	if !TaskStatus(row.Status).Terminal() {
		return Payload{}, ErrTaskNotFinished
	}
	if row.Encoding == nil {
		return Payload{}, fmt.Errorf("%w: task %s", ErrNoResult, row.Status)
	}
	return Payload{Encoding: Encoding(*row.Encoding), Data: row.Data, Display: row.Display}, nil
}

// RerunTask submits the task's input again, with the same upstream tasks, to
// the release it ran on. Admission applies as for any submit.
func (e *Execution) RerunTask(ctx context.Context, workspace identity.WorkspaceID, id TaskID) (Task, error) {
	row, err := e.queries.TaskForRerun(ctx, TaskForRerunParams{ID: uuid.UUID(id), WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return Task{}, ErrNotFound
	}
	if err != nil {
		return Task{}, fmt.Errorf("read task: %w", err)
	}
	deps := make([]TaskID, len(row.DependsOn))
	for n, d := range row.DependsOn {
		deps[n] = TaskID(d)
	}
	release := row.ReleaseID
	tasks, err := e.Submit(ctx, SubmitRequest{
		Workspace: workspace, App: row.AppName, Function: row.FunctionName, Release: &release,
		Inputs: []TaskInput{{Payload: Payload{Encoding: Encoding(row.Encoding), Data: row.Data}, DependsOn: deps}},
	})
	if err != nil {
		return Task{}, err
	}
	return tasks[0], nil
}
