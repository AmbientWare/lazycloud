-- name: DeclaredDisk :one
-- A disk the container's release declares, while the container runs on host.
select c.workspace_id, (d.spec ->> 'size_bytes')::bigint as size_bytes
from containers c
join releases r on r.id = c.release_id
cross join lateral jsonb_array_elements(coalesce(r.spec -> 'disks', '[]'::jsonb)) as d(spec)
where c.id = @container_id and c.host_id = @host_id and c.state in ('starting', 'ready', 'draining')
  and d.spec ->> 'name' = @name::text;

-- name: InsertDisk :exec
insert into disks (workspace_id, name, size_bytes) values (@workspace_id, @name, @size_bytes)
on conflict (workspace_id, name) where state = 'active' do nothing;

-- name: LockActiveDisk :one
-- The disk with its holder's state: the holder keeps the disk until it is
-- released or its container stopped with its host lost.
select d.id, d.size_bytes, d.generation, d.index_sha256, d.holder_container_id, d.lease_token,
       c.state as holder_state, c.stop_reason as holder_stop_reason, h.state as holder_host_state, d.released_at
from disks d
left join containers c on c.id = d.holder_container_id
left join hosts h on h.id = c.host_id
where d.workspace_id = @workspace_id and d.name = @name and d.state = 'active'
for update of d;

-- name: GrowDisk :exec
update disks set size_bytes = @size_bytes, updated_at = now()
where id = @id and size_bytes < @size_bytes;

-- name: TakeDiskLease :exec
update disks
set holder_container_id = @container_id, lease_token = @lease_token, released_at = null,
    failed_operation = null, failure_message = null, failed_at = null, updated_at = now()
where id = @id;

-- name: LockLeasedDisk :one
-- The disk only while container holds it with token: it has not stopped,
-- or it stopped on a live host and has not released the disk yet, which is
-- when its host publishes the final generation.
select d.id, d.generation, d.index_sha256, d.workspace_id, b.bucket, b.region, w.connection_id
from disks d
join containers c on c.id = d.holder_container_id
join hosts h on h.id = c.host_id
join workspaces w on w.id = d.workspace_id
left join workspace_buckets b on b.workspace_id = d.workspace_id
where d.id = @id and d.holder_container_id = @container_id and d.lease_token = @lease_token
  and d.state = 'active' and c.host_id = @host_id and h.state not in ('lost', 'retired')
  and (c.state <> 'stopped' or (d.released_at is null and c.stop_reason is distinct from 'host_lost'))
for update of d;

-- name: AdvanceDisk :exec
update disks
set generation = @generation, index_sha256 = @index_sha256, stored_bytes = stored_bytes + @added_bytes,
    failed_operation = null, failure_message = null, failed_at = null, updated_at = now()
where id = @id;

-- name: ReleaseDisk :execrows
update disks
set released_at = now(), failed_operation = null, failure_message = null, failed_at = null, updated_at = now()
where id = @id and holder_container_id = @container_id and lease_token = @lease_token;

-- name: SetDiskFailure :execrows
-- Records or, with a null operation, clears the holder's last failure. A
-- released lease has nothing left to fail.
update disks
set failed_operation = sqlc.narg(operation)::text, failure_message = sqlc.narg(message)::text,
    failed_at = case when sqlc.narg(operation)::text is null then null else now() end, updated_at = now()
where id = @id and holder_container_id = @container_id and lease_token = @lease_token and released_at is null;

-- name: ShrinkDiskStored :exec
update disks set stored_bytes = greatest(stored_bytes - @removed_bytes, 0), updated_at = now()
where id = @id and holder_container_id = @container_id and lease_token = @lease_token;

-- name: ListDisks :many
select d.id, d.name, d.size_bytes, d.stored_bytes, d.generation, d.holder_container_id,
       c.state as holder_state, c.stop_reason as holder_stop_reason, h.state as holder_host_state, d.released_at,
       d.created_at, d.updated_at, d.failed_operation, d.failure_message, d.failed_at,
       a.name as holder_app, w.kind as holder_kind, w.name as holder_workload
from disks d
left join containers c on c.id = d.holder_container_id
left join hosts h on h.id = c.host_id
left join releases r on r.id = c.release_id
left join workloads w on w.id = r.workload_id
left join apps a on a.id = w.app_id
where d.workspace_id = @workspace_id and d.state = 'active' and d.name > @after::text
order by d.name
limit @max_rows;

-- name: ActiveDisk :one
select d.id, d.name, d.size_bytes, d.stored_bytes, d.generation, d.holder_container_id,
       c.state as holder_state, c.stop_reason as holder_stop_reason, h.state as holder_host_state, d.released_at,
       d.created_at, d.updated_at, d.failed_operation, d.failure_message, d.failed_at,
       a.name as holder_app, w.kind as holder_kind, w.name as holder_workload
from disks d
left join containers c on c.id = d.holder_container_id
left join hosts h on h.id = c.host_id
left join releases r on r.id = c.release_id
left join workloads w on w.id = r.workload_id
left join apps a on a.id = w.app_id
where d.workspace_id = @workspace_id and d.state = 'active' and d.name = @name;

-- name: MarkDiskDeleting :exec
update disks set state = 'deleting', deleted_at = now(), updated_at = now() where id = @id;

-- name: DeletingDisks :many
select d.id, b.bucket, b.region, w.connection_id
from disks d
join workspaces w on w.id = d.workspace_id
left join workspace_buckets b on b.workspace_id = d.workspace_id
where d.state = 'deleting'
order by d.deleted_at
limit @max_rows;

-- name: DeleteDiskRow :exec
delete from disks where id = @id and state = 'deleting';

-- name: WorkspaceDiskBytes :one
-- The declared size of the workspace's live disks other than name, and of
-- name itself.
select coalesce(sum(size_bytes) filter (where name <> @name::text), 0)::bigint as others,
       coalesce(max(size_bytes) filter (where name = @name::text), 0)::bigint as own
from disks
where workspace_id = @workspace_id and state = 'active';

-- name: DeclaredDiskGrowth :one
-- What the workspace's live disks would declare once each named disk is at
-- least its given size, and which named disks would be created or grown.
with declared as (
    select unnest(@names::text[]) as name, unnest(@sizes::bigint[]) as size_bytes
), live as (
    select name, size_bytes from disks where workspace_id = @workspace_id and state = 'active'
)
select coalesce(sum(greatest(l.size_bytes, d.size_bytes)), 0)::bigint as total_bytes,
       coalesce(array_agg(d.name order by d.name) filter (where d.size_bytes > coalesce(l.size_bytes, 0)), '{}')::text[] as growing
from live l
full join declared d on d.name = l.name;
