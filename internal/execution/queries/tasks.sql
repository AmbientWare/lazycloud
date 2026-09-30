-- name: TaskView :one
select t.id, a.name as app_name, w.name as function_name, t.release_id, t.status,
       t.attempt_count, t.created_at, t.started_at, t.finished_at, t.failure
from tasks t
join workloads w on w.id = t.workload_id
join apps a on a.id = w.app_id
where t.id = @id and t.workspace_id = @workspace_id;

-- name: TaskResult :one
select t.status, r.encoding, r.data
from tasks t
left join task_results r on r.task_id = t.id
where t.id = @id and t.workspace_id = @workspace_id;

-- name: TaskStatus :one
select status from tasks where id = @id and workspace_id = @workspace_id;

-- name: TaskLogsAfter :many
select id, attempt, stream, data, logged_at
from task_logs
where task_id = @task_id and id > @after
order by id
limit @max_entries;

-- name: InsertLogs :many
-- Lines keep their order. Lines for attempts outside the container or host
-- are dropped. Returns the tasks that received lines.
with line as (
    select i as ord, (@attempt_ids::uuid[])[i] as attempt_id, (@streams::text[])[i] as stream,
           (@data::text[])[i] as data, (@logged_at::timestamptz[])[i] as logged_at
    from generate_subscripts(@attempt_ids::uuid[], 1) as i
), inserted as (
    insert into task_logs (task_id, attempt, stream, data, logged_at)
    select a.task_id, a.number, line.stream, line.data, line.logged_at
    from line
    join attempts a on a.id = line.attempt_id
    join containers c on c.id = a.container_id
    where c.id = @container_id and c.host_id = @host_id
    order by line.ord
    returning task_id
)
select distinct task_id from inserted;
