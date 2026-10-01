-- name: TaskForTimeline :one
select t.status, t.attempt_count, t.available_at, t.created_at, t.finished_at, now()::timestamptz as observed_at
from tasks t
where t.id = @id and t.workspace_id = @workspace_id;

-- name: TaskAttempts :many
select id, number, container_id, state, started_at, finished_at
from attempts
where task_id = @task_id
order by number;

-- name: TaskRoot :one
select coalesce(root_task_id, id)::uuid as root_id
from tasks
where id = @id and workspace_id = @workspace_id;

-- name: CallGraph :many
-- The root and every task naming it, from the tasks_root index, oldest first
-- so parents precede children.
select t.id, t.parent_task_id, a.name as app_name, w.name as function_name, t.status,
       t.created_at, t.started_at, t.finished_at,
       array(select at.container_id from attempts at
             where at.task_id = t.id and at.number = t.attempt_count)::uuid[] as container_ids,
       array(select d.depends_on from task_dependencies d where d.task_id = t.id order by d.depends_on)::uuid[] as depends_on
from tasks t
join workloads w on w.id = t.workload_id
join apps a on a.id = w.app_id
where t.workspace_id = @workspace_id and (t.id = @root_id or t.root_task_id = @root_id)
order by t.id
limit @max_rows;
