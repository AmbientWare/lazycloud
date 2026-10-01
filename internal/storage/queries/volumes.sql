-- name: WorkspaceBucket :one
select bucket from workspace_buckets where workspace_id = @workspace_id;

-- name: InsertWorkspaceBucket :exec
insert into workspace_buckets (workspace_id, bucket) values (@workspace_id, @bucket)
on conflict (workspace_id) do nothing;

-- name: InsertVolume :exec
insert into volumes (workspace_id, name) values (@workspace_id, @name)
on conflict (workspace_id, name) where state = 'active' do nothing;

-- name: ActiveVolume :one
select id, name, size_bytes, size_measured_at, created_at
from volumes
where workspace_id = @workspace_id and name = @name and state = 'active';

-- name: LockActiveVolume :one
select id from volumes
where workspace_id = @workspace_id and name = @name and state = 'active'
for update;

-- name: ShareActiveVolume :one
select id from volumes
where workspace_id = @workspace_id and name = @name and state = 'active'
for share;

-- name: ListVolumes :many
select id, name, size_bytes, size_measured_at, created_at
from volumes
where workspace_id = @workspace_id and state = 'active' and name > @after::text
order by name
limit @max_rows;

-- name: VolumeUsers :many
-- Workloads whose active release mounts one of the named platform volumes.
select (m.spec ->> 'name')::text as volume, a.name as app, w.kind, w.name as workload
from apps a
join workloads w on w.app_id = a.id
join releases r on r.id = w.active_release_id
cross join lateral jsonb_array_elements(coalesce(r.spec -> 'volumes', '[]'::jsonb)) as m(spec)
where a.workspace_id = @workspace_id and w.desired_state = 'active'
  and m.spec -> 'cloud_bucket' is null
  and (m.spec ->> 'name') = any(@names::text[])
order by a.name, w.kind, w.name;

-- name: LiveMountOfVolume :one
-- A container that mounts the volume and has not stopped.
select c.id
from volume_mounts vm
join containers c on c.id = vm.container_id
where vm.volume_id = @volume_id and c.state <> 'stopped'
limit 1;

-- name: MarkVolumeDeleting :exec
update volumes set state = 'deleting', deleted_at = now() where id = @id;

-- name: InsertVolumeMount :exec
insert into volume_mounts (volume_id, container_id) values (@volume_id, @container_id)
on conflict do nothing;

-- name: DeletingVolumes :many
select v.id, v.workspace_id, coalesce(b.bucket, '')::text as bucket
from volumes v
left join workspace_buckets b on b.workspace_id = v.workspace_id
where v.state = 'deleting'
order by v.deleted_at
limit @max_rows;

-- name: DeleteVolumeRow :exec
delete from volumes where id = @id and state = 'deleting';

-- name: VolumesToMeasure :many
select v.id, b.bucket
from volumes v
join workspace_buckets b on b.workspace_id = v.workspace_id
where v.state = 'active'
  and (v.size_measured_at is null or v.size_measured_at < now() - make_interval(secs => @every_seconds::float8))
order by v.size_measured_at nulls first
limit @max_rows;

-- name: RecordVolumeSize :exec
update volumes set size_bytes = @size_bytes, size_measured_at = now() where id = @id;

-- name: HostMountWorkspaces :many
-- Workspaces whose volumes the host's live containers mount, or whose disks
-- its containers hold and have not released, which includes a stopped
-- holder still publishing its final generation.
select c.workspace_id
from containers c
where c.host_id = @host_id and c.state <> 'stopped'
  and exists (select 1 from volume_mounts vm where vm.container_id = c.id)
union
select c.workspace_id
from disks d
join containers c on c.id = d.holder_container_id
where c.host_id = @host_id and d.released_at is null and d.state = 'active'
  and (c.state <> 'stopped' or c.stop_reason is distinct from 'host_lost');

-- name: InsertStorageGrant :exec
insert into storage_grants (access_key_id, workspace_id, host_id, expires_at)
values (@access_key_id, @workspace_id, @host_id, @expires_at);

-- name: ExpiredStorageGrants :many
select access_key_id from storage_grants
where expires_at < now()
order by expires_at
limit @max_rows;

-- name: DeleteStorageGrant :exec
delete from storage_grants where access_key_id = @access_key_id;

-- name: ClaimOrphanCheck :one
-- A workspace bucket whose orphaned prefixes were not checked for an hour;
-- the claim lasts an hour, so schedulers check different buckets.
update workspace_buckets
set orphans_checked_at = now()
where workspace_id = (
    select b.workspace_id from workspace_buckets b
    where b.orphans_checked_at is null or b.orphans_checked_at < now() - interval '1 hour'
    order by b.orphans_checked_at nulls first
    limit 1
    for update skip locked
)
returning workspace_id, bucket;

-- name: KnownVolumes :many
select id from volumes where id = any(@ids::uuid[]);

-- name: KnownDisks :many
select id from disks where id = any(@ids::uuid[]);
