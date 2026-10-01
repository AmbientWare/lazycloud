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
select d.id, d.size_bytes, d.generation, d.holder_container_id, d.lease_token,
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
set holder_container_id = @container_id, lease_token = @lease_token, released_at = null, updated_at = now()
where id = @id;

-- name: DiskChain :many
-- Generations from the newest parentless one to the newest.
select g.generation, g.manifest_key, g.manifest_sha256
from disk_generations g
where g.disk_id = @disk_id
  and g.generation >= coalesce((
      select max(b.generation) from disk_generations b where b.disk_id = @disk_id and b.parent_generation = 0
  ), 1)
order by g.generation;

-- name: LockLeasedDisk :one
-- The disk only while container holds it with token: it has not stopped,
-- or it stopped on a live host and has not released the disk yet, which is
-- when its host publishes the final generation.
select d.id, d.generation
from disks d
join containers c on c.id = d.holder_container_id
join hosts h on h.id = c.host_id
where d.id = @id and d.holder_container_id = @container_id and d.lease_token = @lease_token
  and d.state = 'active' and c.host_id = @host_id and h.state not in ('lost', 'retired')
  and (c.state <> 'stopped' or (d.released_at is null and c.stop_reason is distinct from 'host_lost'))
for update of d;

-- name: DiskGeneration :one
select manifest_sha256 from disk_generations where disk_id = @disk_id and generation = @generation;

-- name: InsertDiskGeneration :exec
insert into disk_generations (disk_id, generation, parent_generation, manifest_key, manifest_sha256, flat)
values (@disk_id, @generation, @parent_generation, @manifest_key, @manifest_sha256, @flat);

-- name: AdvanceDisk :exec
update disks
set generation = @generation, stored_bytes = stored_bytes + @added_bytes, updated_at = now()
where id = @id;

-- name: ReleaseDisk :execrows
update disks set released_at = now(), updated_at = now()
where id = @id and holder_container_id = @container_id and lease_token = @lease_token;

-- name: ShrinkDiskStored :exec
update disks set stored_bytes = greatest(stored_bytes - @removed_bytes, 0), updated_at = now()
where id = @id and holder_container_id = @container_id and lease_token = @lease_token;

-- name: DeleteDiskGenerationsBefore :exec
delete from disk_generations where disk_id = @disk_id and generation < @generation;

-- name: ListDisks :many
select d.id, d.name, d.size_bytes, d.stored_bytes, d.generation, d.holder_container_id,
       c.state as holder_state, c.stop_reason as holder_stop_reason, h.state as holder_host_state, d.released_at,
       d.created_at, d.updated_at
from disks d
left join containers c on c.id = d.holder_container_id
left join hosts h on h.id = c.host_id
where d.workspace_id = @workspace_id and d.state = 'active' and d.name > @after::text
order by d.name
limit @max_rows;

-- name: ActiveDisk :one
select d.id, d.name, d.size_bytes, d.stored_bytes, d.generation, d.holder_container_id,
       c.state as holder_state, c.stop_reason as holder_stop_reason, h.state as holder_host_state, d.released_at,
       d.created_at, d.updated_at
from disks d
left join containers c on c.id = d.holder_container_id
left join hosts h on h.id = c.host_id
where d.workspace_id = @workspace_id and d.state = 'active' and d.name = @name;

-- name: MarkDiskDeleting :exec
update disks set state = 'deleting', deleted_at = now(), updated_at = now() where id = @id;

-- name: DeletingDisks :many
select d.id, coalesce(b.bucket, '')::text as bucket
from disks d
left join workspace_buckets b on b.workspace_id = d.workspace_id
where d.state = 'deleting'
order by d.deleted_at
limit @max_rows
for update of d skip locked;

-- name: DeleteDiskRow :exec
delete from disks where id = @id and state = 'deleting';
