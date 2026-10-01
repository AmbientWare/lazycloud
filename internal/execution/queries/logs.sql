-- name: TaskStatus :one
select status from tasks where id = @id and workspace_id = @workspace_id;

-- name: WorkloadInWorkspace :one
select w.id from workloads w join apps a on a.id = w.app_id
where w.id = @id and a.workspace_id = @workspace_id;

-- name: ContainerStateInWorkspace :one
select state from containers where id = @id and workspace_id = @workspace_id;

-- name: TaskLogsAfter :many
select id, task_id, attempt, stream, data, logged_at
from task_logs
where task_id = @key and id > @after
order by id
limit @max_entries;

-- name: WorkloadLogsAfter :many
select id, task_id, attempt, stream, data, logged_at
from task_logs
where workload_id = @key and id > @after
order by id
limit @max_entries;

-- name: ContainerLogsAfter :many
select id, task_id, attempt, stream, data, logged_at
from task_logs
where container_id = @key and id > @after
order by id
limit @max_entries;

-- name: TaskLogTail :one
-- The cursor just before the last @tail entries.
select coalesce(min(id) - 1, 0)::bigint from (
    select id from task_logs where task_id = @key order by id desc limit @tail
) last;

-- name: WorkloadLogTail :one
select coalesce(min(id) - 1, 0)::bigint from (
    select id from task_logs where workload_id = @key order by id desc limit @tail
) last;

-- name: ContainerLogTail :one
select coalesce(min(id) - 1, 0)::bigint from (
    select id from task_logs where container_id = @key order by id desc limit @tail
) last;

-- name: InsertLogs :many
-- Lines keep their order. Lines for attempts outside the container or host
-- are dropped. Returns the tasks and the workload that received lines.
with line as (
    select i as ord, (@attempt_ids::uuid[])[i] as attempt_id, (@streams::text[])[i] as stream,
           (@data::text[])[i] as data, (@logged_at::timestamptz[])[i] as logged_at
    from generate_subscripts(@attempt_ids::uuid[], 1) as i
), inserted as (
    insert into task_logs (task_id, attempt, stream, data, logged_at, workload_id, container_id)
    select a.task_id, a.number, line.stream, line.data, line.logged_at, r.workload_id, c.id
    from line
    join attempts a on a.id = line.attempt_id
    join containers c on c.id = a.container_id
    join releases r on r.id = c.release_id
    where c.id = @container_id and c.host_id = @host_id
    order by line.ord
    returning task_id, workload_id
)
select distinct task_id, workload_id from inserted;
