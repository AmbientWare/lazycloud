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

// ChannelContainerLog wakes followers of a release's container output; the
// payload is the release id.
const ChannelContainerLog database.Channel = "lc_container_log"

// ContainerLogEntry is a stored line of a container's own output.
type ContainerLogEntry struct {
	ID     int64
	Stream LogStream
	Data   string
	Time   time.Time
}

// ContainerLogLine is a line a container wrote outside attempts, for the
// HTTP request it served when Request is set.
type ContainerLogLine struct {
	LogLine
	Request *uuid.UUID
}

// AppendContainerLogs stores output a container wrote outside attempts, in
// order. Lines of a container not assigned to host are dropped.
func (e *Execution) AppendContainerLogs(ctx context.Context, host compute.HostID, container ContainerID, lines []ContainerLogLine) error {
	if len(lines) == 0 {
		return nil
	}
	params := InsertContainerLogsParams{
		ContainerID: uuid.UUID(container),
		HostID:      hostUUID(host),
		Streams:     make([]string, len(lines)),
		Data:        make([]string, len(lines)),
		LoggedAt:    make([]time.Time, len(lines)),
		Requests:    make([]uuid.UUID, len(lines)),
	}
	for n, line := range lines {
		params.Streams[n] = string(line.Stream)
		// PostgreSQL text cannot hold NUL.
		params.Data[n] = strings.ReplaceAll(line.Data, "\x00", "�")
		params.LoggedAt[n] = line.Time
		if line.Request != nil {
			params.Requests[n] = *line.Request
		}
	}
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		releases, err := e.queries.WithTx(tx).InsertContainerLogs(ctx, params)
		if err != nil {
			return fmt.Errorf("insert container logs: %w", err)
		}
		return database.NotifyAll(ctx, tx, ChannelContainerLog, uuidStrings(releases))
	})
	if err != nil {
		return fmt.Errorf("append container logs: %w", err)
	}
	return nil
}

// PruneContainerLogs deletes up to limit lines written before before, the
// oldest first, and returns how many it deleted.
func (e *Execution) PruneContainerLogs(ctx context.Context, before time.Time, limit int) (int, error) {
	n, err := e.queries.PruneContainerLogs(ctx, PruneContainerLogsParams{Before: before, MaxRows: int32(limit)}) //nolint:gosec // bounded by the caller
	if err != nil {
		return 0, fmt.Errorf("prune container logs: %w", err)
	}
	return int(n), nil
}

// RequestLogs returns what was written while serving an HTTP request of the
// workspace, after the cursor, in order.
func (e *Execution) RequestLogs(ctx context.Context, workspace uuid.UUID, request uuid.UUID, after int64, limit int) ([]ContainerLogEntry, error) {
	rows, err := e.queries.RequestLogsAfter(ctx, RequestLogsAfterParams{
		RequestID: &request, WorkspaceID: workspace, After: after, MaxEntries: int32(min(limit, 1000)), //nolint:gosec // bounded
	})
	if err != nil {
		return nil, fmt.Errorf("read request logs: %w", err)
	}
	out := make([]ContainerLogEntry, len(rows))
	for n, row := range rows {
		out[n] = ContainerLogEntry{ID: row.ID, Stream: LogStream(row.Stream), Data: row.Data, Time: row.LoggedAt}
	}
	return out, nil
}

// ReleaseContainer is the newest live container of a release.
type ReleaseContainer struct {
	ID    ContainerID
	State ContainerState
	Host  *uuid.UUID
}

// NewestContainer returns the release's newest live container, or false.
func (e *Execution) NewestContainer(ctx context.Context, release uuid.UUID) (ReleaseContainer, bool, error) {
	row, err := e.queries.ReleaseContainer(ctx, release)
	if errors.Is(err, pgx.ErrNoRows) {
		return ReleaseContainer{}, false, nil
	}
	if err != nil {
		return ReleaseContainer{}, false, fmt.Errorf("read release container: %w", err)
	}
	return ReleaseContainer{ID: ContainerID(row.ID), State: ContainerState(row.State), Host: row.HostID}, true, nil
}

// StreamReleaseLogs passes the output of the release's containers after the
// cursor to emit in batches. With follow it waits for more until done
// reports true or ctx ends, and emits an empty batch whenever heartbeat
// passes without one. The listener must listen on ChannelContainerLog.
func (e *Execution) StreamReleaseLogs(ctx context.Context, listener *database.Listener, release uuid.UUID, after int64, follow bool, heartbeat time.Duration, done func(context.Context) (bool, error), emit func([]ContainerLogEntry) error) error {
	var wake <-chan struct{}
	var idle *time.Timer
	if follow {
		var cancel func()
		wake, cancel = listener.Subscribe(ChannelContainerLog, release.String())
		defer cancel()
		idle = time.NewTimer(heartbeat)
		defer idle.Stop()
	}
	for {
		// Whether the stream may end is read before the entries, so entries
		// written before the end are all read.
		finished := !follow
		if follow {
			var err error
			if finished, err = done(ctx); err != nil {
				return err
			}
		}
		for {
			rows, err := e.queries.ReleaseLogsAfter(ctx, ReleaseLogsAfterParams{ReleaseID: release, After: after, MaxEntries: logBatch})
			if err != nil {
				return fmt.Errorf("read container logs: %w", err)
			}
			if len(rows) > 0 {
				batch := make([]ContainerLogEntry, len(rows))
				for n, row := range rows {
					batch[n] = ContainerLogEntry{ID: row.ID, Stream: LogStream(row.Stream), Data: row.Data, Time: row.LoggedAt}
				}
				if err := emit(batch); err != nil {
					return err
				}
				if idle != nil {
					idle.Reset(heartbeat)
				}
				after = rows[len(rows)-1].ID
			}
			if len(rows) < logBatch {
				break
			}
		}
		if finished {
			return nil
		}
		select {
		case <-wake:
		case <-idle.C:
			if err := emit(nil); err != nil {
				return err
			}
			idle.Reset(heartbeat)
		case <-ctx.Done():
			return nil
		}
	}
}

// StreamContainerOutput passes what a container wrote outside attempts
// after the cursor to emit in batches. With follow it waits for more until
// the container has stopped and its output is drained, or ctx ends, and
// emits an empty batch whenever heartbeat passes without one.
func (e *Execution) StreamContainerOutput(ctx context.Context, listener *database.Listener, workspace identity.WorkspaceID, container ContainerID, after int64, follow bool, heartbeat time.Duration, emit func([]ContainerLogEntry) error) error {
	route, err := e.Route(ctx, container)
	if err != nil {
		return err
	}
	if route.Workspace != workspace {
		return ErrNotFound
	}
	release, err := e.queries.ContainerReleaseOf(ctx, uuid.UUID(container))
	if err != nil {
		return fmt.Errorf("read container release: %w", err)
	}
	done := func(ctx context.Context) (bool, error) {
		r, err := e.Route(ctx, container)
		if err != nil {
			return false, err
		}
		return r.State == ContainerStopped, nil
	}
	var wake <-chan struct{}
	var idle *time.Timer
	if follow {
		var cancel func()
		wake, cancel = listener.Subscribe(ChannelContainerLog, release.String())
		defer cancel()
		idle = time.NewTimer(heartbeat)
		defer idle.Stop()
	}
	poll := time.NewTicker(time.Second)
	defer poll.Stop()
	for {
		finished := !follow
		if follow {
			if finished, err = done(ctx); err != nil {
				return err
			}
		}
		for {
			rows, err := e.queries.ContainerOutputAfter(ctx, ContainerOutputAfterParams{ContainerID: uuid.UUID(container), After: after, MaxEntries: logBatch})
			if err != nil {
				return fmt.Errorf("read container output: %w", err)
			}
			if len(rows) > 0 {
				batch := make([]ContainerLogEntry, len(rows))
				for n, row := range rows {
					batch[n] = ContainerLogEntry{ID: row.ID, Stream: LogStream(row.Stream), Data: row.Data, Time: row.LoggedAt}
				}
				if err := emit(batch); err != nil {
					return err
				}
				if idle != nil {
					idle.Reset(heartbeat)
				}
				after = rows[len(rows)-1].ID
			}
			if len(rows) < logBatch {
				break
			}
		}
		if finished {
			return nil
		}
		// A stop sends no output wake; the poll sees it.
		select {
		case <-wake:
		case <-poll.C:
		case <-idle.C:
			if err := emit(nil); err != nil {
				return err
			}
			idle.Reset(heartbeat)
		case <-ctx.Done():
			return nil
		}
	}
}
