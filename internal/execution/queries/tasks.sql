-- name: TaskView :one
select t.id, a.name as app_name, w.name as function_name, t.release_id, r.version, t.status,
       t.attempt_count, t.max_attempts, t.parent_task_id, t.root_task_id, t.available_at,
       t.created_at, t.started_at, t.finished_at, t.failure, t.scheduled_for,
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
       t.created_at, t.started_at, t.finished_at, t.failure, t.scheduled_for,
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
  and (not @root_only::bool or t.parent_task_id is null)
  and (sqlc.narg(search)::text is null
       or starts_with(t.id::text, sqlc.narg(search)::text)
       or strpos(lower(w.name), sqlc.narg(search)::text) > 0)
  and t.id < @before
order by t.id desc
limit @max_rows;

-- name: ListAppTasks :many
-- Like ListTasks for one app's functions: each workload's recent index
-- yields at most a page, and the pages merge.
select t.id, a.name as app_name, w.name as function_name, t.release_id, r.version, t.status,
       t.attempt_count, t.max_attempts, t.parent_task_id, t.root_task_id, t.available_at,
       t.created_at, t.started_at, t.finished_at, t.failure, t.scheduled_for,
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
      and (not @root_only::bool or t.parent_task_id is null)
      and (sqlc.narg(search)::text is null
           or starts_with(t.id::text, sqlc.narg(search)::text)
           or strpos(lower(w.name), sqlc.narg(search)::text) > 0)
      and t.id < @before
    order by t.id desc
    limit @max_rows
) t
join releases r on r.id = t.release_id
where a.workspace_id = @workspace_id and a.id = @app_id
  and (sqlc.narg(function)::text is null or w.name = sqlc.narg(function)::text)
  and (sqlc.narg(version)::int is null or r.version = sqlc.narg(version)::int)
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
           count(*) filter (where c.state = 'pending' and c.capacity_wait = 'provisioning') as provisioning,
           count(*) filter (where c.state = 'pending' and c.capacity_wait = 'limit') as limited,
           count(*) filter (where c.state = 'ready') as ready
    from containers c
    where c.release_id in (select release_id from task) and c.state <> 'stopped'
    group by c.release_id
)
select task.id, task.unmet_dependencies, task.attempt_count, task.available_at, task.created_at,
       task.last_finished_at, live.starting_since, live.unplaced_since,
       coalesce(live.ready, 0)::int as ready, coalesce(live.provisioning, 0)::int as provisioning,
       coalesce(live.limited, 0)::int as limited, now()::timestamptz as observed_at
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
select t.status, r.encoding, r.data, r.display
from tasks t
left join task_results r on r.task_id = t.id
where t.id = @id and t.workspace_id = @workspace_id;

-- name: MissingTasks :many
-- The ids, in order, that name no task in the workspace.
select req.id::uuid as id
from unnest(@ids::uuid[]) with ordinality as req(id, n)
where not exists (select 1 from tasks t where t.id = req.id and t.workspace_id = @workspace_id)
order by req.n;

-- name: FinishedTasks :many
-- The finished tasks among ids, in request order. A result inlines when its
-- size as the API encodes it without HTML escaping (base64 for cloudpickle,
-- plus the display, whose U+2028 and U+2029 take three bytes more) is at
-- most @result_max and the inlined sizes so far stay within @total_max; only
-- those rows read the result bytes.
with finished as (
    select req.n, t.id, t.workload_id, t.release_id, t.status, t.attempt_count, t.max_attempts,
           t.parent_task_id, t.root_task_id, t.available_at, t.created_at, t.started_at,
           t.finished_at, t.failure, t.scheduled_for, res.encoding,
           case res.encoding when 'cloudpickle' then (octet_length(res.data) + 2) / 3 * 4
                else octet_length(res.data) end
             + coalesce(octet_length(shown.text)
                        + 3 * (char_length(shown.text) - char_length(translate(shown.text, U&'\2028\2029', ''))),
                        0) as result_size
    from unnest(@ids::uuid[]) with ordinality as req(id, n)
    join tasks t on t.id = req.id
    left join task_results res on res.task_id = t.id
    left join lateral (select res.display::text as text) shown on true
    where t.workspace_id = @workspace_id and t.status in ('succeeded', 'failed', 'cancelled')
), budget as (
    select f.*,
           f.result_size <= @result_max::bigint
             and sum(case when f.result_size <= @result_max::bigint then f.result_size else 0 end)
                   over (order by f.n) <= @total_max::bigint as inline
    from finished f
)
select b.id, a.name as app_name, w.name as function_name, b.release_id, r.version, b.status,
       b.attempt_count, b.max_attempts, b.parent_task_id, b.root_task_id, b.available_at,
       b.created_at, b.started_at, b.finished_at, b.failure, b.scheduled_for,
       array(select at.container_id from attempts at
             where at.task_id = b.id and at.number = b.attempt_count)::uuid[] as container_ids,
       b.encoding, coalesce(b.inline, false)::bool as inline, d.data, d.display
from budget b
join workloads w on w.id = b.workload_id
join apps a on a.id = w.app_id
join releases r on r.id = b.release_id
left join task_results d on b.inline and d.task_id = b.id
order by b.n;
