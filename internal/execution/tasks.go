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

// TaskNotFoundError names a task the workspace does not have. It matches
// ErrNotFound.
type TaskNotFoundError struct {
	Task TaskID
}

func (e *TaskNotFoundError) Error() string {
	return fmt.Sprintf("task %s is not in the workspace", e.Task)
}

func (e *TaskNotFoundError) Unwrap() error { return ErrNotFound }

// FinishedTask is a finished task with its result when the result comes
// inline.
type FinishedTask struct {
	Task   Task
	Result *Payload
	// ResultOmitted means the task has a result that did not fit the inline
	// budget; TaskResult reads it.
	ResultOmitted bool
}

const (
	// maxInlineResult bounds one result inlined by WaitTasks, as the API
	// encodes it.
	maxInlineResult = 256 << 10
	// maxInlineResults bounds all results inlined by one WaitTasks call.
	maxInlineResults = 4 << 20
)

// WaitTasks returns the finished tasks among ids, in the order of ids. With
// wait above zero it holds until at least one has finished, wait passes or
// ctx ends, and returns none when none finished. Every id must name a task in
// workspace; the first that does not fails the call with a
// *TaskNotFoundError.
func (e *Execution) WaitTasks(ctx context.Context, listener *database.Listener, workspace identity.WorkspaceID, ids []TaskID, wait time.Duration) ([]FinishedTask, error) {
	keys := make([]uuid.UUID, len(ids))
	for n, id := range ids {
		keys[n] = uuid.UUID(id)
	}
	var wake <-chan struct{}
	if wait > 0 {
		// Subscribe before the first read so a finish between a read and
		// the wait still wakes this call.
		var cancel func()
		wake, cancel = listener.Subscribe(database.ChannelTask, uuidStrings(keys)...)
		defer cancel()
	}
	if err := e.checkTasksExist(ctx, workspace, keys); err != nil {
		return nil, err
	}
	timer := time.NewTimer(wait)
	defer timer.Stop()
	for {
		finished, err := e.finishedTasks(ctx, workspace, keys)
		if err != nil || len(finished) > 0 || wait <= 0 {
			return finished, err
		}
		select {
		case <-wake:
		case <-timer.C:
			return finished, nil
		case <-ctx.Done():
			return finished, nil
		}
	}
}

func (e *Execution) checkTasksExist(ctx context.Context, workspace identity.WorkspaceID, ids []uuid.UUID) error {
	unknown, err := e.queries.FirstUnknownTask(ctx, FirstUnknownTaskParams{Ids: ids, WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("check tasks: %w", err)
	}
	return &TaskNotFoundError{Task: TaskID(unknown)}
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
		task, err := taskFrom(TaskViewRow{
			ID: row.ID, AppName: row.AppName, FunctionName: row.FunctionName, ReleaseID: row.ReleaseID,
			Version: row.Version, Status: row.Status, AttemptCount: row.AttemptCount, MaxAttempts: row.MaxAttempts,
			ParentTaskID: row.ParentTaskID, RootTaskID: row.RootTaskID, AvailableAt: row.AvailableAt,
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
	row, err := e.queries.TaskView(ctx, TaskViewParams{ID: uuid.UUID(id), WorkspaceID: uuid.UUID(workspace)})
	if errors.Is(err, pgx.ErrNoRows) {
		return Task{}, ErrNotFound
	}
	if err != nil {
		return Task{}, fmt.Errorf("read task: %w", err)
	}
	task, err := taskFrom(row)
	if err != nil {
		return Task{}, err
	}
	tasks := []Task{task}
	if err := e.addPending(ctx, tasks); err != nil {
		return Task{}, err
	}
	return tasks[0], nil
}

func taskFrom(row TaskViewRow) (Task, error) {
	task := Task{
		ID: TaskID(row.ID), App: row.AppName, Function: row.FunctionName, Release: row.ReleaseID,
		Version: versionOf(row.Version), Status: TaskStatus(row.Status),
		Attempts: int(row.AttemptCount), MaxAttempts: int(row.MaxAttempts),
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
	var status *string
	if filter.Status != nil {
		s := string(*filter.Status)
		status = &s
	}
	var search *string
	if filter.Search != nil && *filter.Search != "" {
		// LIKE wildcards in the search are literal characters.
		lowered := strings.ToLower(*filter.Search)
		search = &lowered
	}
	size := pageSize(limit)
	var rows []TaskViewRow
	if filter.App != nil {
		app, err := e.queries.LiveAppID(ctx, LiveAppIDParams{WorkspaceID: uuid.UUID(workspace), Name: *filter.App})
		if errors.Is(err, pgx.ErrNoRows) {
			return TaskPage{Tasks: []Task{}}, nil
		}
		if err != nil {
			return TaskPage{}, fmt.Errorf("find app: %w", err)
		}
		r, err := e.queries.ListAppTasks(ctx, ListAppTasksParams{
			WorkspaceID: uuid.UUID(workspace), AppID: app, Function: filter.Function, Status: status,
			RootOnly: filter.RootOnly, Search: search, Version: int32Of(filter.Version), Before: before, MaxRows: size + 1,
		})
		if err != nil {
			return TaskPage{}, fmt.Errorf("list app tasks: %w", err)
		}
		for _, row := range r {
			rows = append(rows, TaskViewRow(row))
		}
	} else {
		r, err := e.queries.ListTasks(ctx, ListTasksParams{
			WorkspaceID: uuid.UUID(workspace), Status: status, RootOnly: filter.RootOnly, Search: search,
			Before: before, MaxRows: size + 1,
		})
		if err != nil {
			return TaskPage{}, fmt.Errorf("list tasks: %w", err)
		}
		for _, row := range r {
			rows = append(rows, TaskViewRow(row))
		}
	}
	var page TaskPage
	if len(rows) > int(size) {
		rows = rows[:size]
		page.Next = rows[len(rows)-1].ID.String()
	}
	page.Tasks = make([]Task, len(rows))
	for n, row := range rows {
		var err error
		if page.Tasks[n], err = taskFrom(row); err != nil {
			return TaskPage{}, err
		}
	}
	if err := e.addPending(ctx, page.Tasks); err != nil {
		return TaskPage{}, err
	}
	return page, nil
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
