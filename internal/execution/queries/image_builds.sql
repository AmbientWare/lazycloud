-- name: CreateBuildContainer :one
insert into containers (workspace_id, image_build_id, state, slots, cpu_millis, memory_bytes)
values (@workspace_id, @image_build_id, 'pending', 1, @cpu_millis, @memory_bytes)
returning id;

-- name: BuildContainers :many
-- A build's containers, oldest first; the last one is the current attempt.
select id, state, host_id, stop_reason, exit_message
from containers
where image_build_id = @image_build_id
order by created_at, id;

-- name: DrainBuildContainers :many
-- Live build containers stop claiming nothing and are stopped by their host.
update containers
set state = 'draining', drain_started_at = now()
where image_build_id = @image_build_id and state in ('starting', 'ready')
returning id, host_id;

-- name: StopPendingBuildContainers :exec
update containers
set state = 'stopped', stop_reason = 'stopped', exit_message = 'build ended', stopped_at = now()
where image_build_id = @image_build_id and state = 'pending';

-- name: StartingBuildContainersOnHost :many
select c.id, c.image_build_id::uuid as image_build_id, c.cpu_millis, c.memory_bytes,
       (select count(*) from containers p where p.image_build_id = c.image_build_id and p.created_at <= c.created_at)::int as attempt
from containers c
where c.host_id = @host_id and c.state = 'starting' and c.image_build_id is not null
order by c.id;

-- name: LiveBuildContainer :one
-- The build attempt a live container on the host runs.
select c.image_build_id::uuid as image_build_id,
       (select count(*) from containers p where p.image_build_id = c.image_build_id and p.created_at <= c.created_at)::int as attempt
from containers c
where c.id = @id and c.host_id = @host_id and c.state <> 'stopped' and c.image_build_id is not null;
