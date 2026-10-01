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
)

// Task is a task as callers see it.
type Task struct {
	ID         TaskID
	App        string
	Function   string
	Release    uuid.UUID
	Status     TaskStatus
	Attempts   int
	CreatedAt  time.Time
	StartedAt  *time.Time
	FinishedAt *time.Time
	Failure    *Failure
	// Parent is the task that spawned this one; Root is the root of its
	// call graph, the task itself when nothing spawned it.
	Parent *TaskID
	Root   TaskID
	// ScheduledFor is the cron occurrence that admitted the task.
	ScheduledFor *time.Time
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
	task := Task{
		ID: TaskID(row.ID), App: row.AppName, Function: row.FunctionName, Release: row.ReleaseID,
		Status: TaskStatus(row.Status), Attempts: int(row.AttemptCount),
		CreatedAt: row.CreatedAt, StartedAt: row.StartedAt, FinishedAt: row.FinishedAt,
		Parent: (*TaskID)(row.ParentTaskID), Root: TaskID(row.RootTaskID), ScheduledFor: row.ScheduledFor,
	}
	if row.Failure != nil {
		task.Failure = &Failure{}
		if err := json.Unmarshal(row.Failure, task.Failure); err != nil {
			return Task{}, fmt.Errorf("decode task failure: %w", err)
		}
	}
	return task, nil
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
	return Payload{Encoding: Encoding(*row.Encoding), Data: row.Data}, nil
}

// LogStream names a task output stream.
type LogStream string

const (
	LogStdout LogStream = "stdout"
	LogStderr LogStream = "stderr"
	LogSystem LogStream = "system"
)

// LogLine is output a host reports for an attempt.
type LogLine struct {
	Attempt AttemptID
	Stream  LogStream
	Data    string
	Time    time.Time
}

// LogEntry is a stored log line.
type LogEntry struct {
	ID      int64
	Attempt int
	Stream  LogStream
	Data    string
	Time    time.Time
}

// AppendLogs stores lines in order in one statement. Lines for attempts that
// do not run on container, or a container not assigned to host, are dropped.
// Followers of each task wake on commit.
func (e *Execution) AppendLogs(ctx context.Context, host compute.HostID, container ContainerID, lines []LogLine) error {
	if len(lines) == 0 {
		return nil
	}
	params := InsertLogsParams{
		AttemptIds:  make([]uuid.UUID, len(lines)),
		Streams:     make([]string, len(lines)),
		Data:        make([]string, len(lines)),
		LoggedAt:    make([]time.Time, len(lines)),
		ContainerID: uuid.UUID(container),
		HostID:      hostUUID(host),
	}
	for n, line := range lines {
		params.AttemptIds[n] = uuid.UUID(line.Attempt)
		params.Streams[n] = string(line.Stream)
		// PostgreSQL text cannot hold NUL.
		params.Data[n] = strings.ReplaceAll(line.Data, "\x00", "�")
		params.LoggedAt[n] = line.Time
	}
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		tasks, err := e.queries.WithTx(tx).InsertLogs(ctx, params)
		if err != nil {
			return fmt.Errorf("insert logs: %w", err)
		}
		for _, task := range tasks {
			if err := database.Notify(ctx, tx, database.ChannelTask, task.String()); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("append logs: %w", err)
	}
	return nil
}

// logBatch bounds one read of a task's log.
const logBatch = 500

// StreamLogs passes the task's log entries after the cursor to emit in
// batches. Without follow it ends once the stored entries are drained. With
// follow it waits for more until the task is terminal and drained, or ctx
// ends, and emits an empty batch whenever heartbeat passes without one, so
// the transport can show the stream is alive. It returns ErrNotFound before
// emitting anything for an unknown task.
func (e *Execution) StreamLogs(ctx context.Context, listener *database.Listener, workspace identity.WorkspaceID, id TaskID, after int64, follow bool, heartbeat time.Duration, emit func([]LogEntry) error) error {
	var wake <-chan struct{}
	var idle *time.Timer
	if follow {
		var cancel func()
		wake, cancel = listener.Subscribe(database.ChannelTask, id.String())
		defer cancel()
		idle = time.NewTimer(heartbeat)
		defer idle.Stop()
	}
	send := func(batch []LogEntry) error {
		if err := emit(batch); err != nil {
			return err
		}
		if idle != nil {
			idle.Reset(heartbeat)
		}
		return nil
	}
	for {
		// Read the status before the entries: entries a terminal task wrote
		// are all visible to the read that follows.
		status, err := e.queries.TaskStatus(ctx, TaskStatusParams{ID: uuid.UUID(id), WorkspaceID: uuid.UUID(workspace)})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("read task status: %w", err)
		}
		for {
			rows, err := e.queries.TaskLogsAfter(ctx, TaskLogsAfterParams{TaskID: uuid.UUID(id), After: after, MaxEntries: logBatch})
			if err != nil {
				return fmt.Errorf("read task logs: %w", err)
			}
			if len(rows) > 0 {
				batch := make([]LogEntry, len(rows))
				for n, row := range rows {
					batch[n] = LogEntry{ID: row.ID, Attempt: int(row.Attempt), Stream: LogStream(row.Stream), Data: row.Data, Time: row.LoggedAt}
				}
				if err := send(batch); err != nil {
					return err
				}
				after = rows[len(rows)-1].ID
			}
			if len(rows) < logBatch {
				break
			}
		}
		if !follow || TaskStatus(status).Terminal() {
			return nil
		}
		select {
		case <-wake:
		case <-idle.C:
			if err := send(nil); err != nil {
				return err
			}
		case <-ctx.Done():
			return nil
		}
	}
}

func hostUUID(host compute.HostID) *uuid.UUID {
	id := uuid.UUID(host)
	return &id
}
