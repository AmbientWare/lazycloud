-- name: ListContainers :many
-- Newest first below the cursor, from the workspace's recent index.
select c.id, a.name as app_name, w.name as function_name, r.id as release_id, r.version, c.state,
       c.stop_reason, c.exit_message, c.slots, c.cpu_millis, c.memory_bytes,
       c.created_at, c.ready_at, c.stopped_at,
       (select count(*) from attempts at where at.container_id = c.id and at.state = 'running')::int as running
from containers c
join releases r on r.id = c.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
where c.workspace_id = @workspace_id
  and c.id < @before
order by c.id desc
limit @max_rows;

-- name: ListLiveContainers :many
-- Like ListContainers for containers that have not stopped, from the
-- workspace's live partial index.
select c.id, a.name as app_name, w.name as function_name, r.id as release_id, r.version, c.state,
       c.stop_reason, c.exit_message, c.slots, c.cpu_millis, c.memory_bytes,
       c.created_at, c.ready_at, c.stopped_at,
       (select count(*) from attempts at where at.container_id = c.id and at.state = 'running')::int as running
from containers c
join releases r on r.id = c.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
where c.workspace_id = @workspace_id
  and c.state <> 'stopped'
  and c.id < @before
order by c.id desc
limit @max_rows;

-- name: ContainerView :one
select c.id, a.name as app_name, w.name as function_name, r.id as release_id, r.version, c.state,
       c.stop_reason, c.exit_message, c.slots, c.cpu_millis, c.memory_bytes,
       c.created_at, c.ready_at, c.stopped_at,
       (select count(*) from attempts at where at.container_id = c.id and at.state = 'running')::int as running
from containers c
join releases r on r.id = c.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
where c.workspace_id = @workspace_id and c.id = @id;

-- name: LockContainerInWorkspace :one
-- Function containers only; image builds stop through the images owner.
select id, state, host_id, release_id::uuid as release_id from containers
where id = @id and workspace_id = @workspace_id and release_id is not null
for update;

-- name: DrainContainer :exec
update containers set state = 'draining', drain_started_at = now()
where id = @id and state in ('starting', 'ready');

-- name: LiveFunctionContainers :many
select id from containers
where workspace_id = @workspace_id and state <> 'stopped' and release_id is not null
order by id
limit @row_limit;
