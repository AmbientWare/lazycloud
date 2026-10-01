package storage

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

const (
	// MaxValueBytes bounds one queue message or map value.
	MaxValueBytes = 1 << 20
	// MaxMapTTL is the longest a map key can live before it expires.
	MaxMapTTL = 7 * 24 * time.Hour
)

// ChannelQueue wakes pops waiting on a queue; the payload is the queue id.
const ChannelQueue database.Channel = "lc_queue"

func isForeignKeyViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23503"
}

// queueID returns the queue's id, creating the queue when create is set. A
// missing queue without create is ErrNotFound.
func (s *Storage) queueID(ctx context.Context, workspace identity.WorkspaceID, name string, create bool) (uuid.UUID, error) {
	id, err := s.queries.QueueID(ctx, QueueIDParams{WorkspaceID: uuid.UUID(workspace), Name: name})
	if err == nil {
		return id, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return uuid.UUID{}, fmt.Errorf("read queue: %w", err)
	}
	if !create {
		return uuid.UUID{}, ErrNotFound
	}
	id, err = s.queries.InsertQueue(ctx, InsertQueueParams{WorkspaceID: uuid.UUID(workspace), Name: name})
	if err != nil {
		return uuid.UUID{}, fmt.Errorf("create queue: %w", err)
	}
	return id, nil
}

// PutQueueMessages appends messages in order and wakes waiting pops.
func (s *Storage) PutQueueMessages(ctx context.Context, workspace identity.WorkspaceID, name string, messages [][]byte) (apitypes.QueueInfo, error) {
	for _, m := range messages {
		if len(m) > MaxValueBytes {
			return apitypes.QueueInfo{}, ErrTooLarge
		}
	}
	// A delete between reading the id and inserting fails the insert's
	// foreign key; the queue is then created again once.
	for attempt := 0; ; attempt++ {
		id, err := s.queueID(ctx, workspace, name, true)
		if err != nil {
			return apitypes.QueueInfo{}, err
		}
		err = pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
			if err := s.queries.WithTx(tx).InsertQueueMessages(ctx, InsertQueueMessagesParams{QueueID: id, Messages: messages}); err != nil {
				return fmt.Errorf("insert messages: %w", err)
			}
			return database.Notify(ctx, tx, ChannelQueue, id.String())
		})
		if isForeignKeyViolation(err) && attempt == 0 {
			continue
		}
		if err != nil {
			return apitypes.QueueInfo{}, fmt.Errorf("put queue messages: %w", err)
		}
		return s.queueInfo(ctx, name, id)
	}
}

func (s *Storage) queueInfo(ctx context.Context, name string, id uuid.UUID) (apitypes.QueueInfo, error) {
	stats, err := s.queries.QueueStats(ctx, id)
	if err != nil {
		return apitypes.QueueInfo{}, fmt.Errorf("read queue size: %w", err)
	}
	return apitypes.QueueInfo{Name: name, Size: stats.Size, OldestMessageAt: optionalTime(stats.Oldest)}, nil
}

// GetQueue returns the queue's size; a queue never written is empty.
func (s *Storage) GetQueue(ctx context.Context, workspace identity.WorkspaceID, name string) (apitypes.QueueInfo, error) {
	id, err := s.queueID(ctx, workspace, name, false)
	if errors.Is(err, ErrNotFound) {
		return apitypes.QueueInfo{Name: name}, nil
	}
	if err != nil {
		return apitypes.QueueInfo{}, err
	}
	return s.queueInfo(ctx, name, id)
}

// PopQueueMessage removes and returns the oldest message. With wait it
// waits up to wait for a put; found is false when the queue stayed empty.
// listener must listen on ChannelQueue when wait is positive.
func (s *Storage) PopQueueMessage(ctx context.Context, listener *database.Listener, workspace identity.WorkspaceID, name string, wait time.Duration) (message []byte, found bool, err error) {
	deadline := time.Now().Add(wait)
	for {
		id, err := s.queueID(ctx, workspace, name, wait > 0)
		if errors.Is(err, ErrNotFound) {
			return nil, false, nil
		}
		if err != nil {
			return nil, false, err
		}
		var woken <-chan struct{}
		unsubscribe := func() {}
		if wait > 0 {
			// Subscribe before reading so a put after the read wakes us.
			woken, unsubscribe = listener.Subscribe(ChannelQueue, id.String())
		}
		data, err := s.queries.PopQueueMessage(ctx, id)
		if err == nil {
			unsubscribe()
			return data, true, nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			unsubscribe()
			return nil, false, fmt.Errorf("pop queue message: %w", err)
		}
		remaining := time.Until(deadline)
		if remaining <= 0 {
			unsubscribe()
			return nil, false, nil
		}
		timer := time.NewTimer(remaining)
		select {
		case <-ctx.Done():
			timer.Stop()
			unsubscribe()
			return nil, false, fmt.Errorf("pop queue message: %w", ctx.Err())
		case <-woken:
		case <-timer.C:
		}
		timer.Stop()
		unsubscribe()
	}
}

// PeekQueueMessage returns the oldest message without removing it.
func (s *Storage) PeekQueueMessage(ctx context.Context, workspace identity.WorkspaceID, name string) ([]byte, bool, error) {
	id, err := s.queueID(ctx, workspace, name, false)
	if errors.Is(err, ErrNotFound) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	data, err := s.queries.PeekQueueMessage(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("peek queue message: %w", err)
	}
	return data, true, nil
}

// ListQueues returns a page of queues by name.
func (s *Storage) ListQueues(ctx context.Context, workspace identity.WorkspaceID, cursor string, limit int) (apitypes.QueuePage, error) {
	rows, err := s.queries.ListQueues(ctx, ListQueuesParams{WorkspaceID: uuid.UUID(workspace), After: cursor, MaxRows: int32(limit)}) //nolint:gosec // The schema caps limit.
	if err != nil {
		return apitypes.QueuePage{}, fmt.Errorf("list queues: %w", err)
	}
	page := apitypes.QueuePage{Queues: make([]apitypes.QueueInfo, len(rows))}
	for n, row := range rows {
		page.Queues[n] = apitypes.QueueInfo{Name: row.Name, Size: row.Size, OldestMessageAt: optionalTime(row.Oldest)}
	}
	if len(rows) == limit {
		page.NextCursor = &rows[len(rows)-1].Name
	}
	return page, nil
}

// DeleteQueue deletes the queue and its messages. A missing queue is
// already deleted.
func (s *Storage) DeleteQueue(ctx context.Context, workspace identity.WorkspaceID, name string) error {
	if err := s.queries.DeleteQueue(ctx, DeleteQueueParams{WorkspaceID: uuid.UUID(workspace), Name: name}); err != nil {
		return fmt.Errorf("delete queue: %w", err)
	}
	return nil
}

func (s *Storage) mapID(ctx context.Context, q *Queries, workspace identity.WorkspaceID, name string, create bool) (uuid.UUID, error) {
	id, err := q.MapID(ctx, MapIDParams{WorkspaceID: uuid.UUID(workspace), Name: name})
	if err == nil {
		return id, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return uuid.UUID{}, fmt.Errorf("read map: %w", err)
	}
	if !create {
		return uuid.UUID{}, ErrNotFound
	}
	id, err = q.InsertMap(ctx, InsertMapParams{WorkspaceID: uuid.UUID(workspace), Name: name})
	if err != nil {
		return uuid.UUID{}, fmt.Errorf("create map: %w", err)
	}
	return id, nil
}

func revisionOut(r int64) string { return strconv.FormatInt(r, 10) }

func parseRevision(r *string) (*int64, error) {
	if r == nil {
		return nil, nil
	}
	v, err := strconv.ParseInt(*r, 10, 64)
	if err != nil {
		return nil, invalid("revision %q is not one this map returned", *r)
	}
	return &v, nil
}

// SetMapEntry is a map write.
type SetMapEntry struct {
	Value []byte
	// TTL is the time to live; zero never expires; nil keeps an existing
	// key's expiry and gives a new key none.
	TTL        *time.Duration
	IfRevision *string
	IfAbsent   bool
}

// SetMapEntry writes a key, creating the map on first use. The entry row
// lock serializes conditional writes of one key.
func (s *Storage) SetMapEntry(ctx context.Context, workspace identity.WorkspaceID, name, key string, w SetMapEntry) (apitypes.MapEntryWrite, error) {
	if len(w.Value) > MaxValueBytes {
		return apitypes.MapEntryWrite{}, ErrTooLarge
	}
	if w.TTL != nil && (*w.TTL < 0 || *w.TTL > MaxMapTTL) {
		return apitypes.MapEntryWrite{}, invalid("ttl must be between 0 and %d seconds", int(MaxMapTTL.Seconds()))
	}
	if w.IfAbsent && w.IfRevision != nil {
		return apitypes.MapEntryWrite{}, invalid("if_absent and if_revision cannot both be set")
	}
	want, err := parseRevision(w.IfRevision)
	if err != nil {
		return apitypes.MapEntryWrite{}, err
	}
	var out apitypes.MapEntryWrite
	for attempt := 0; ; attempt++ {
		err = pgx.BeginFunc(ctx, s.pool, func(tx pgx.Tx) error {
			q := s.queries.WithTx(tx)
			id, err := s.mapID(ctx, q, workspace, name, true)
			if err != nil {
				return err
			}
			current, err := q.LockMapEntry(ctx, LockMapEntryParams{MapID: id, Key: key})
			present := err == nil && !current.Expired
			if err != nil && !errors.Is(err, pgx.ErrNoRows) {
				return fmt.Errorf("lock map entry: %w", err)
			}
			switch {
			case w.IfAbsent && present:
				return conflict("key %q already exists", key)
			case want != nil && (!present || current.Revision != *want):
				return conflict("key %q changed or expired since revision %s", key, *w.IfRevision)
			}
			var expires *time.Time
			switch {
			case w.TTL == nil && present:
				expires = current.ExpiresAt
			case w.TTL != nil && *w.TTL > 0:
				at := time.Now().Add(*w.TTL)
				expires = &at
			}
			params := UpsertMapEntryParams{MapID: id, Key: key, Data: w.Value, ExpiresAt: expires}
			var row UpsertMapEntryRow
			if w.IfAbsent {
				// No row was there to lock, so the insert itself decides.
				var inserted InsertMapEntryIfAbsentRow
				inserted, err = q.InsertMapEntryIfAbsent(ctx, InsertMapEntryIfAbsentParams(params))
				if errors.Is(err, pgx.ErrNoRows) {
					return conflict("key %q already exists", key)
				}
				row = UpsertMapEntryRow(inserted)
			} else {
				row, err = q.UpsertMapEntry(ctx, params)
			}
			if err != nil {
				return fmt.Errorf("write map entry: %w", err)
			}
			out = apitypes.MapEntryWrite{Revision: revisionOut(row.Revision), ExpiresAt: row.ExpiresAt}
			return nil
		})
		// A delete of the map between reading its id and writing fails the
		// foreign key; the map is created again once.
		if isForeignKeyViolation(err) && attempt == 0 {
			continue
		}
		if err != nil {
			return apitypes.MapEntryWrite{}, fmt.Errorf("set map entry: %w", err)
		}
		return out, nil
	}
}

// GetMapEntry returns a live key.
func (s *Storage) GetMapEntry(ctx context.Context, workspace identity.WorkspaceID, name, key string) (apitypes.MapEntry, error) {
	id, err := s.mapID(ctx, s.queries, workspace, name, false)
	if err != nil {
		return apitypes.MapEntry{}, err
	}
	row, err := s.queries.MapEntry(ctx, MapEntryParams{MapID: id, Key: key})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.MapEntry{}, ErrNotFound
	}
	if err != nil {
		return apitypes.MapEntry{}, fmt.Errorf("read map entry: %w", err)
	}
	return apitypes.MapEntry{
		Key: key, Value: row.Data, Revision: revisionOut(row.Revision), ExpiresAt: row.ExpiresAt, UpdatedAt: row.UpdatedAt,
	}, nil
}

// DeleteMapEntry deletes a live key, only at ifRevision when given.
func (s *Storage) DeleteMapEntry(ctx context.Context, workspace identity.WorkspaceID, name, key string, ifRevision *string) error {
	want, err := parseRevision(ifRevision)
	if err != nil {
		return err
	}
	id, err := s.mapID(ctx, s.queries, workspace, name, false)
	if err != nil {
		return err
	}
	deleted, err := s.queries.DeleteMapEntry(ctx, DeleteMapEntryParams{MapID: id, Key: key, IfRevision: want})
	if err != nil {
		return fmt.Errorf("delete map entry: %w", err)
	}
	if deleted > 0 {
		return nil
	}
	if want != nil {
		exists, err := s.queries.MapEntryExists(ctx, MapEntryExistsParams{MapID: id, Key: key})
		if err != nil {
			return fmt.Errorf("read map entry: %w", err)
		}
		if exists {
			return conflict("key %q changed since revision %s", key, *ifRevision)
		}
	}
	return ErrNotFound
}

// ListMapKeys returns live keys in byte order.
func (s *Storage) ListMapKeys(ctx context.Context, workspace identity.WorkspaceID, name, prefix, cursor string, limit int) (apitypes.MapKeyPage, error) {
	id, err := s.mapID(ctx, s.queries, workspace, name, false)
	if errors.Is(err, ErrNotFound) {
		return apitypes.MapKeyPage{Keys: []string{}}, nil
	}
	if err != nil {
		return apitypes.MapKeyPage{}, err
	}
	keys, err := s.queries.ListMapKeys(ctx, ListMapKeysParams{MapID: id, After: cursor, Prefix: prefix, MaxRows: int32(limit)}) //nolint:gosec // The schema caps limit.
	if err != nil {
		return apitypes.MapKeyPage{}, fmt.Errorf("list map keys: %w", err)
	}
	page := apitypes.MapKeyPage{Keys: keys}
	if page.Keys == nil {
		page.Keys = []string{}
	}
	if len(keys) == limit {
		page.NextCursor = &keys[len(keys)-1]
	}
	return page, nil
}

// GetMap returns the map's live key statistics; a map never written is
// empty.
func (s *Storage) GetMap(ctx context.Context, workspace identity.WorkspaceID, name string) (apitypes.MapInfo, error) {
	id, err := s.mapID(ctx, s.queries, workspace, name, false)
	if errors.Is(err, ErrNotFound) {
		return apitypes.MapInfo{Name: name}, nil
	}
	if err != nil {
		return apitypes.MapInfo{}, err
	}
	stats, err := s.queries.MapStats(ctx, id)
	if err != nil {
		return apitypes.MapInfo{}, fmt.Errorf("read map: %w", err)
	}
	return apitypes.MapInfo{
		Name: name, Count: stats.Count, SizeBytes: stats.SizeBytes, ExpiringCount: stats.ExpiringCount,
		NextExpiryAt: optionalTime(stats.NextExpiryAt),
	}, nil
}

// ListMaps returns a page of maps with live keys, by name.
func (s *Storage) ListMaps(ctx context.Context, workspace identity.WorkspaceID, cursor string, limit int) (apitypes.MapPage, error) {
	rows, err := s.queries.ListMaps(ctx, ListMapsParams{WorkspaceID: uuid.UUID(workspace), After: cursor, MaxRows: int32(limit)}) //nolint:gosec // The schema caps limit.
	if err != nil {
		return apitypes.MapPage{}, fmt.Errorf("list maps: %w", err)
	}
	page := apitypes.MapPage{Maps: make([]apitypes.MapInfo, len(rows))}
	for n, row := range rows {
		page.Maps[n] = apitypes.MapInfo{
			Name: row.Name, Count: row.Count, SizeBytes: row.SizeBytes, ExpiringCount: row.ExpiringCount,
			NextExpiryAt: optionalTime(row.NextExpiryAt),
		}
	}
	if len(rows) == limit {
		page.NextCursor = &rows[len(rows)-1].Name
	}
	return page, nil
}

// DeleteMap deletes the map and its keys. A missing map is already deleted.
func (s *Storage) DeleteMap(ctx context.Context, workspace identity.WorkspaceID, name string) error {
	if err := s.queries.DeleteMap(ctx, DeleteMapParams{WorkspaceID: uuid.UUID(workspace), Name: name}); err != nil {
		return fmt.Errorf("delete map: %w", err)
	}
	return nil
}
