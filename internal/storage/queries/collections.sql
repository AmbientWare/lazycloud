-- name: QueueID :one
select id from queues where workspace_id = @workspace_id and name = @name;

-- name: InsertQueue :one
insert into queues (workspace_id, name) values (@workspace_id, @name)
on conflict (workspace_id, name) do update set name = excluded.name
returning id;

-- name: InsertQueueMessages :exec
insert into queue_messages (queue_id, data)
select @queue_id, m.data from unnest(@messages::bytea[]) with ordinality as m(data, n)
order by m.n;

-- name: PopQueueMessage :one
-- The oldest message no concurrent pop holds.
delete from queue_messages
where (queue_id, id) = (
    select q.queue_id, q.id from queue_messages q
    where q.queue_id = @queue_id
    order by q.id
    limit 1
    for update skip locked
)
returning data;

-- name: PeekQueueMessage :one
select data from queue_messages where queue_id = @queue_id order by id limit 1;

-- name: QueueStats :one
select count(*)::bigint as size, min(created_at) as oldest
from queue_messages where queue_id = @queue_id;

-- name: ListQueues :many
select q.name, s.size, s.oldest
from queues q
-- A listing counts at most 100,001 messages per queue, so one huge
-- queue does not slow every page.
cross join lateral (
    select count(*)::bigint as size, min(m.created_at) as oldest
    from (select created_at from queue_messages m where m.queue_id = q.id order by m.id limit 100001) m
) s
where q.workspace_id = @workspace_id and q.name > @after::text
order by q.name
limit @max_rows;

-- name: DeleteQueue :exec
delete from queues where workspace_id = @workspace_id and name = @name;

-- name: MapID :one
select id from maps where workspace_id = @workspace_id and name = @name;

-- name: InsertMap :one
insert into maps (workspace_id, name) values (@workspace_id, @name)
on conflict (workspace_id, name) do update set name = excluded.name
returning id;

-- name: MapEntry :one
select data, revision, expires_at, updated_at
from map_entries
where map_id = @map_id and key = @key and (expires_at is null or expires_at > now());

-- name: LockMapEntry :one
-- The entry with its expiry; an expired entry counts as missing.
select revision, expires_at, (expires_at is not null and expires_at <= now())::boolean as expired
from map_entries
where map_id = @map_id and key = @key
for update;

-- name: UpsertMapEntry :one
insert into map_entries (map_id, key, data, revision, expires_at)
values (@map_id, @key, @data, nextval('map_entry_revisions'), @expires_at)
on conflict (map_id, key) do update
set data = excluded.data, revision = excluded.revision, expires_at = excluded.expires_at, updated_at = now()
returning revision, expires_at;

-- name: DeleteMapEntry :execrows
delete from map_entries
where map_id = @map_id and key = @key and (expires_at is null or expires_at > now())
  and (sqlc.narg(if_revision)::bigint is null or revision = sqlc.narg(if_revision));

-- name: MapEntryExists :one
select exists (
    select 1 from map_entries
    where map_id = @map_id and key = @key and (expires_at is null or expires_at > now())
);

-- name: ListMapKeys :many
-- Keys compare in byte order (collate "C"), so a prefix is a range scan.
select key from map_entries
where map_id = @map_id and key > @after::text
  and (@prefix::text = '' or starts_with(key, @prefix::text))
  and (expires_at is null or expires_at > now())
order by key
limit @max_rows;

-- name: MapStats :one
select count(*)::bigint as count,
       coalesce(sum(length(data)), 0)::bigint as size_bytes,
       count(expires_at)::bigint as expiring_count,
       min(expires_at) as next_expiry_at
from map_entries
where map_id = @map_id and (expires_at is null or expires_at > now());

-- name: ListMaps :many
select m.name, s.count, s.size_bytes, s.expiring_count, s.next_expiry_at
from maps m
-- A listing counts at most 100,001 live entries per map.
cross join lateral (
    select count(*)::bigint as count,
           coalesce(sum(length(e.data)), 0)::bigint as size_bytes,
           count(e.expires_at)::bigint as expiring_count,
           min(e.expires_at) as next_expiry_at
    from (
        select data, expires_at from map_entries e
        where e.map_id = m.id and (e.expires_at is null or e.expires_at > now())
        limit 100001
    ) e
) s
where m.workspace_id = @workspace_id and m.name > @after::text and s.count > 0
order by m.name
limit @max_rows;

-- name: DeleteMap :exec
delete from maps where workspace_id = @workspace_id and name = @name;

-- name: SweepExpiredMapEntries :execrows
delete from map_entries
where ctid = any(array(
    select ctid from map_entries
    where expires_at is not null and expires_at <= now()
    limit @max_rows
    for update skip locked
));

-- name: InsertMapEntryIfAbsent :one
-- Writes only when the key is missing or expired; no row means it exists.
insert into map_entries (map_id, key, data, revision, expires_at)
values (@map_id, @key, @data, nextval('map_entry_revisions'), @expires_at)
on conflict (map_id, key) do update
set data = excluded.data, revision = excluded.revision, expires_at = excluded.expires_at, updated_at = now()
where map_entries.expires_at is not null and map_entries.expires_at <= now()
returning revision, expires_at;
