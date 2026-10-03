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

	"github.com/AmbientWare/lazycloud/internal/compute"
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

// NewestTasks is when each live function of app last had a task submitted,
// by function name: nil for one without tasks, and no entry for a function
// or app not deployed.
func (e *Execution) NewestTasks(ctx context.Context, workspace identity.WorkspaceID, app string) (map[string]*time.Time, error) {
	rows, err := e.queries.AppFunctionsNewestTask(ctx, AppFunctionsNewestTaskParams{WorkspaceID: uuid.UUID(workspace), App: app})
	if err != nil {
		return nil, fmt.Errorf("read newest tasks of app %s: %w", app, err)
	}
	out := make(map[string]*time.Time, len(rows))
	for _, r := range rows {
		out[r.Name] = r.Newest
	}
	return out, nil
}

// TaskLatency is where one task's time went: submitted, then its current
// attempt's container created and placed on a host, then finished. A time
// is nil before it happens.
type TaskLatency struct {
	Task             TaskID
	Status           TaskStatus
	Submitted        time.Time
	ContainerCreated *time.Time
	Placed           *time.Time
	Finished         *time.Time
	// HostReady is when the container's host last became ready; nil unless
	// it still serves.
	HostReady *time.Time
	// InstanceType and Market are the host's; empty before placement or on
	// a joined machine.
	InstanceType string
	Market       string
}

// TaskLatencies reads the latency of each of the workspace's tasks ids.
func (e *Execution) TaskLatencies(ctx context.Context, workspace identity.WorkspaceID, ids []TaskID) ([]TaskLatency, error) {
	raw := make([]uuid.UUID, len(ids))
	for n, id := range ids {
		raw[n] = uuid.UUID(id)
	}
	rows, err := e.queries.TaskLatencies(ctx, TaskLatenciesParams{Ids: raw, WorkspaceID: uuid.UUID(workspace)})
	if err != nil {
		return nil, fmt.Errorf("read task latencies: %w", err)
	}
	out := make([]TaskLatency, len(rows))
	for n, r := range rows {
		out[n] = TaskLatency{
			Task: TaskID(r.ID), Status: TaskStatus(r.Status), Submitted: r.CreatedAt, ContainerCreated: r.ContainerCreatedAt,
			Placed: r.AssignedAt, Finished: r.FinishedAt, InstanceType: r.InstanceType, Market: r.Market,
		}
		if r.HostPhase != nil && compute.Phase(*r.HostPhase) == compute.PhaseReady {
			out[n].HostReady = r.HostPhaseAt
		}
	}
	return out, nil
}
