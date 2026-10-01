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

// AppendContainerLogs stores output a container wrote outside attempts, in
// order. Lines of a container not assigned to host are dropped.
func (e *Execution) AppendContainerLogs(ctx context.Context, host compute.HostID, container ContainerID, lines []LogLine) error {
	if len(lines) == 0 {
		return nil
	}
	params := InsertContainerLogsParams{
		ContainerID: uuid.UUID(container),
		HostID:      hostUUID(host),
		Streams:     make([]string, len(lines)),
		Data:        make([]string, len(lines)),
		LoggedAt:    make([]time.Time, len(lines)),
	}
	for n, line := range lines {
		params.Streams[n] = string(line.Stream)
		// PostgreSQL text cannot hold NUL.
		params.Data[n] = strings.ReplaceAll(line.Data, "\x00", "�")
		params.LoggedAt[n] = line.Time
	}
	err := pgx.BeginFunc(ctx, e.pool, func(tx pgx.Tx) error {
		releases, err := e.queries.WithTx(tx).InsertContainerLogs(ctx, params)
		if err != nil {
			return fmt.Errorf("insert container logs: %w", err)
		}
		for _, release := range releases {
			if err := database.Notify(ctx, tx, ChannelContainerLog, release.String()); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return fmt.Errorf("append container logs: %w", err)
	}
	return nil
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
