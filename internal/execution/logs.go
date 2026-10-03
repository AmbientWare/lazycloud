package execution

import (
	"context"
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
	Task    TaskID
	Attempt int
	Stream  LogStream
	Data    string
	Time    time.Time
}

// AppendLogs stores lines in order in one statement. Lines for attempts that
// do not run on container, or a container not assigned to host, are dropped.
// Followers of each task, of the workload and of the container wake on
// commit.
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
		rows, err := e.queries.WithTx(tx).InsertLogs(ctx, params)
		if err != nil {
			return fmt.Errorf("insert logs: %w", err)
		}
		if len(rows) == 0 {
			return nil
		}
		tasks := make([]string, len(rows))
		followers := []string{container.String()}
		for n, row := range rows {
			tasks[n] = row.TaskID.String()
			if n == 0 || row.WorkloadID != rows[0].WorkloadID {
				followers = append(followers, row.WorkloadID.String())
			}
		}
		if err := database.NotifyAll(ctx, tx, database.ChannelTask, tasks); err != nil {
			return err
		}
		return database.NotifyAll(ctx, tx, database.ChannelLogs, followers)
	})
	if err != nil {
		return fmt.Errorf("append logs: %w", err)
	}
	return nil
}

// LogSourceKind selects whose lines a log stream reads.
type LogSourceKind string

const (
	LogsOfTask      LogSourceKind = "task"
	LogsOfWorkload  LogSourceKind = "workload"
	LogsOfContainer LogSourceKind = "container"
)

// LogSource names the task, workload or container whose lines to stream.
type LogSource struct {
	Kind LogSourceKind
	ID   uuid.UUID
}

// LogQuery is where a log stream starts and whether it follows.
type LogQuery struct {
	// After starts after this entry id.
	After int64
	// Tail, when above zero, starts at the last Tail stored entries if
	// that is later than After.
	Tail   int
	Follow bool
	// Heartbeat is the longest a followed stream stays silent; it emits an
	// empty batch then.
	Heartbeat time.Duration
}

// logBatch bounds one read of a log.
const logBatch = 500

// unsettledRetry is how soon a follower reads again after stopping at a line
// whose transaction may still have older lines in flight.
const unsettledRetry = 50 * time.Millisecond

// settleWait bounds how long a read without follow waits for lines in
// flight. A transaction that runs longer holds back every newer line, since
// visibility is cluster-wide.
const settleWait = 2 * time.Second

func sleepCtx(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-t.C:
		return true
	case <-ctx.Done():
		return false
	}
}

// logPosition is a position in (writer, id) order. Writers commit identity
// values out of order, but a transaction that commits after a read has a
// writer at or above the oldest one that was running, so it sorts after
// every settled line the read returned.
type logPosition struct {
	writer int64
	id     int64
}

func (c logPosition) less(o logPosition) bool {
	return c.writer < o.writer || (c.writer == o.writer && c.id < o.id)
}

// StreamLogs passes the source's log entries to emit in batches, in commit
// order. Without follow it ends once the settled entries are drained. A
// followed task stream ends when the task is terminal and drained, a
// container stream when the container has stopped and is drained, and a
// workload stream only when ctx ends. It returns ErrNotFound before
// emitting anything for a source outside the workspace.
func (e *Execution) StreamLogs(ctx context.Context, listener *database.Listener, workspace identity.WorkspaceID, source LogSource, query LogQuery, emit func([]LogEntry) error) error {
	var wake <-chan struct{}
	var idle *time.Timer
	if query.Follow {
		channel := database.ChannelLogs
		if source.Kind == LogsOfTask {
			channel = database.ChannelTask
		}
		var cancel func()
		wake, cancel = listener.Subscribe(channel, source.ID.String())
		defer cancel()
		idle = time.NewTimer(query.Heartbeat)
		defer idle.Stop()
	}
	// Check the source before reading any entry, so an unknown one fails
	// before the stream starts.
	finished, err := e.logSourceFinished(ctx, workspace, source)
	if err != nil {
		return err
	}
	cursor, err := e.startCursor(ctx, source, query)
	if err != nil {
		return err
	}
	send := func(batch []LogEntry) error {
		if err := emit(batch); err != nil {
			return err
		}
		if idle != nil {
			idle.Reset(query.Heartbeat)
		}
		return nil
	}
	settleBy := time.Now().Add(settleWait)
	for {
		page, err := e.drainLogs(ctx, source, cursor, send)
		if err != nil {
			return err
		}
		cursor = page.cursor
		if !query.Follow {
			// A read without follow waits briefly for lines in flight, so it
			// returns what had committed when it began.
			if !page.unsettled || time.Now().After(settleBy) {
				return nil
			}
			if !sleepCtx(ctx, unsettledRetry) {
				return nil
			}
			continue
		}
		if finished && !page.unsettled {
			return nil
		}
		var retry <-chan time.Time
		if page.unsettled {
			retry = time.After(unsettledRetry)
		}
		select {
		case <-wake:
		case <-retry:
		case <-idle.C:
			if err := send(nil); err != nil {
				return err
			}
		case <-ctx.Done():
			return nil
		}
		// Read the state before the entries: entries a finished source
		// wrote are all visible to the read that follows.
		if finished, err = e.logSourceFinished(ctx, workspace, source); err != nil {
			return err
		}
	}
}

// logPage is where a drain of settled entries ended.
type logPage struct {
	cursor logPosition
	// unsettled means the drain stopped at an entry an older running
	// transaction may still precede.
	unsettled bool
}

// drainLogs sends every settled entry after cursor in batches.
func (e *Execution) drainLogs(ctx context.Context, source LogSource, cursor logPosition, send func([]LogEntry) error) (logPage, error) {
	page := logPage{cursor: cursor}
	for {
		rows, err := e.logRows(ctx, source, page.cursor)
		if err != nil {
			return page, err
		}
		batch := make([]LogEntry, 0, len(rows))
		for _, row := range rows {
			if !row.Settled {
				page.unsettled = true
				break
			}
			batch = append(batch, LogEntry{
				ID: row.ID, Task: TaskID(row.TaskID), Attempt: int(row.Attempt), Stream: LogStream(row.Stream),
				Data: row.Data, Time: row.LoggedAt,
			})
			page.cursor = logPosition{writer: row.Writer, id: row.ID}
		}
		if len(batch) > 0 {
			if err := send(batch); err != nil {
				return page, err
			}
		}
		if page.unsettled || len(rows) < logBatch {
			return page, nil
		}
	}
}

// startCursor is where a stream begins: after the entry the client names,
// or at the last query.Tail entries when that is later.
func (e *Execution) startCursor(ctx context.Context, source LogSource, query LogQuery) (logPosition, error) {
	cursor := logPosition{id: query.After}
	if query.After > 0 {
		writer, err := e.queries.LogCursor(ctx, query.After)
		switch {
		case err == nil:
			cursor.writer = writer
		case !errors.Is(err, pgx.ErrNoRows):
			return logPosition{}, fmt.Errorf("read log cursor: %w", err)
		}
	}
	if query.Tail <= 0 {
		return cursor, nil
	}
	n := int32(min(query.Tail, maxPage)) //nolint:gosec // Bounded by maxPage.
	var tail logPosition
	var err error
	switch source.Kind {
	case LogsOfTask:
		var row TaskLogTailRow
		row, err = e.queries.TaskLogTail(ctx, TaskLogTailParams{Key: source.ID, Tail: n})
		tail = logPosition{writer: row.Writer, id: row.ID}
	case LogsOfWorkload:
		var row WorkloadLogTailRow
		row, err = e.queries.WorkloadLogTail(ctx, WorkloadLogTailParams{Key: source.ID, Tail: n})
		tail = logPosition{writer: row.Writer, id: row.ID}
	case LogsOfContainer:
		var row ContainerLogTailRow
		row, err = e.queries.ContainerLogTail(ctx, ContainerLogTailParams{Key: source.ID, Tail: n})
		tail = logPosition{writer: row.Writer, id: row.ID}
	}
	if errors.Is(err, pgx.ErrNoRows) {
		// Fewer lines than the tail: start at the beginning.
		return cursor, nil
	}
	if err != nil {
		return logPosition{}, fmt.Errorf("read log tail: %w", err)
	}
	// Just before the oldest tail line.
	tail.id--
	if cursor.less(tail) {
		return tail, nil
	}
	return cursor, nil
}

// logRows reads up to logBatch entries after cursor in (writer, id) order.
func (e *Execution) logRows(ctx context.Context, source LogSource, cursor logPosition) ([]TaskLogsAfterRow, error) {
	var rows []TaskLogsAfterRow
	var err error
	switch source.Kind {
	case LogsOfTask:
		rows, err = e.queries.TaskLogsAfter(ctx, TaskLogsAfterParams{
			Key: source.ID, AfterWriter: cursor.writer, AfterID: cursor.id, MaxEntries: logBatch,
		})
	case LogsOfWorkload:
		var r []WorkloadLogsAfterRow
		r, err = e.queries.WorkloadLogsAfter(ctx, WorkloadLogsAfterParams{
			Key: source.ID, AfterWriter: cursor.writer, AfterID: cursor.id, MaxEntries: logBatch,
		})
		for _, row := range r {
			rows = append(rows, TaskLogsAfterRow(row))
		}
	case LogsOfContainer:
		var r []ContainerLogsAfterRow
		r, err = e.queries.ContainerLogsAfter(ctx, ContainerLogsAfterParams{
			Key: source.ID, AfterWriter: cursor.writer, AfterID: cursor.id, MaxEntries: logBatch,
		})
		for _, row := range r {
			rows = append(rows, TaskLogsAfterRow(row))
		}
	}
	if err != nil {
		return nil, fmt.Errorf("read logs: %w", err)
	}
	return rows, nil
}

// CheckLogSource returns ErrNotFound unless the source belongs to the
// workspace.
func (e *Execution) CheckLogSource(ctx context.Context, workspace identity.WorkspaceID, source LogSource) error {
	_, err := e.logSourceFinished(ctx, workspace, source)
	return err
}

// logSourceFinished reports whether the source writes no more lines, after
// checking it belongs to the workspace.
func (e *Execution) logSourceFinished(ctx context.Context, workspace identity.WorkspaceID, source LogSource) (bool, error) {
	var finished bool
	var err error
	switch source.Kind {
	case LogsOfTask:
		var status string
		status, err = e.queries.TaskStatus(ctx, TaskStatusParams{ID: source.ID, WorkspaceID: uuid.UUID(workspace)})
		finished = TaskStatus(status).Terminal()
	case LogsOfWorkload:
		_, err = e.queries.WorkloadInWorkspace(ctx, WorkloadInWorkspaceParams{ID: source.ID, WorkspaceID: uuid.UUID(workspace)})
	case LogsOfContainer:
		var state string
		state, err = e.queries.ContainerStateInWorkspace(ctx, ContainerStateInWorkspaceParams{ID: source.ID, WorkspaceID: uuid.UUID(workspace)})
		finished = ContainerState(state) == ContainerStopped
	default:
		return false, fmt.Errorf("unknown log source %q", source.Kind)
	}
	if errors.Is(err, pgx.ErrNoRows) {
		return false, ErrNotFound
	}
	if err != nil {
		return false, fmt.Errorf("read log source: %w", err)
	}
	return finished, nil
}

func hostUUID(host compute.HostID) *uuid.UUID {
	id := uuid.UUID(host)
	return &id
}
