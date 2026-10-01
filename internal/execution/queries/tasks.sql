-- name: TaskView :one
select t.id, a.name as app_name, w.name as function_name, t.release_id, r.version, t.status,
       t.attempt_count, t.max_attempts, t.parent_task_id, t.root_task_id, t.available_at,
       t.created_at, t.started_at, t.finished_at, t.failure,
       -- The latest attempt's container, as zero or one element: a scalar
       -- subquery keeps the per-row index lookup, and the array keeps sqlc
       -- from reading it as non-null.
       array(select at.container_id from attempts at
             where at.task_id = t.id and at.number = t.attempt_count)::uuid[] as container_ids
from tasks t
join workloads w on w.id = t.workload_id
join apps a on a.id = w.app_id
join releases r on r.id = t.release_id
where t.id = @id and t.workspace_id = @workspace_id;

-- name: ListTasks :many
-- Newest first below the cursor, from the workspace's recent index.
select t.id, a.name as app_name, w.name as function_name, t.release_id, r.version, t.status,
       t.attempt_count, t.max_attempts, t.parent_task_id, t.root_task_id, t.available_at,
       t.created_at, t.started_at, t.finished_at, t.failure,
       -- The latest attempt's container, as zero or one element: a scalar
       -- subquery keeps the per-row index lookup, and the array keeps sqlc
       -- from reading it as non-null.
       array(select at.container_id from attempts at
             where at.task_id = t.id and at.number = t.attempt_count)::uuid[] as container_ids
from tasks t
join workloads w on w.id = t.workload_id
join apps a on a.id = w.app_id
join releases r on r.id = t.release_id
where t.workspace_id = @workspace_id
  and (sqlc.narg(status)::text is null or t.status = sqlc.narg(status)::text)
  and t.id < @before
order by t.id desc
limit @max_rows;

-- name: ListAppTasks :many
-- Like ListTasks for one app's functions: each workload's recent index
-- yields at most a page, and the pages merge.
select t.id, a.name as app_name, w.name as function_name, t.release_id, r.version, t.status,
       t.attempt_count, t.max_attempts, t.parent_task_id, t.root_task_id, t.available_at,
       t.created_at, t.started_at, t.finished_at, t.failure,
       -- The latest attempt's container, as zero or one element: a scalar
       -- subquery keeps the per-row index lookup, and the array keeps sqlc
       -- from reading it as non-null.
       array(select at.container_id from attempts at
             where at.task_id = t.id and at.number = t.attempt_count)::uuid[] as container_ids
from workloads w
join apps a on a.id = w.app_id
cross join lateral (
    select * from tasks t
    where t.workload_id = w.id
      and (sqlc.narg(status)::text is null or t.status = sqlc.narg(status)::text)
      and t.id < @before
    order by t.id desc
    limit @max_rows
) t
join releases r on r.id = t.release_id
where a.workspace_id = @workspace_id and a.id = @app_id
  and (sqlc.narg(function)::text is null or w.name = sqlc.narg(function)::text)
order by t.id desc
limit @max_rows;

-- name: LiveAppID :one
select id from apps where workspace_id = @workspace_id and name = @name and state <> 'deleted';

-- name: PendingFacts :many
-- What a queued task waits for, from the release's live containers: the
-- earliest starting and unplaced containers and whether any is ready.
-- Reads the containers_live_release partial index once per release.
with task as (
    select t.id, t.release_id, t.unmet_dependencies, t.attempt_count, t.available_at, t.created_at,
           latest.finished_at as last_finished_at
    from tasks t
    left join attempts latest on latest.task_id = t.id and latest.number = t.attempt_count
    where t.id = any(@ids::uuid[]) and t.status = 'queued'
), live as (
    select c.release_id,
           min(c.assigned_at) filter (where c.state = 'starting')::timestamptz as starting_since,
           min(c.created_at) filter (where c.state = 'pending')::timestamptz as unplaced_since,
           count(*) filter (where c.state = 'ready') as ready
    from containers c
    where c.release_id in (select release_id from task) and c.state <> 'stopped'
    group by c.release_id
)
select task.id, task.unmet_dependencies, task.attempt_count, task.available_at, task.created_at,
       task.last_finished_at, live.starting_since, live.unplaced_since,
       coalesce(live.ready, 0)::int as ready, now()::timestamptz as observed_at
from task
left join live on live.release_id = task.release_id;

-- name: TaskForRerun :one
select t.release_id, a.name as app_name, w.name as function_name, i.encoding, i.data,
       array(select d.depends_on from task_dependencies d where d.task_id = t.id order by d.depends_on)::uuid[] as depends_on
from tasks t
join workloads w on w.id = t.workload_id
join apps a on a.id = w.app_id
join task_inputs i on i.task_id = t.id
where t.id = @id and t.workspace_id = @workspace_id;

-- name: TaskResult :one
select t.status, r.encoding, r.data
from tasks t
left join task_results r on r.task_id = t.id
where t.id = @id and t.workspace_id = @workspace_id;
