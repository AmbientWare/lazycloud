-- name: ScheduleExpression :one
select expression from schedules where workload_id = @workload_id;

-- name: ReplaceSchedule :exec
-- A new expression starts over: its first occurrence follows the deploy.
insert into schedules (workload_id, expression, next_fire_at)
values (@workload_id, @expression, @next_fire_at)
on conflict (workload_id) do update
set expression = excluded.expression,
    next_fire_at = excluded.next_fire_at,
    last_fired_at = null,
    last_task_id = null,
    last_error = null,
    updated_at = now();

-- name: DeleteSchedule :exec
delete from schedules where workload_id = @workload_id;

-- name: LockDueSchedules :many
-- Due schedules with their workload locked, so admission's workload lock is
-- already held and a deploy holding the workload is skipped, not waited on.
select s.workload_id, s.expression, s.next_fire_at, a.workspace_id, a.name as app_name,
       w.name as function_name, now()::timestamptz as now
from schedules s
join workloads w on w.id = s.workload_id
join apps a on a.id = w.app_id
where s.next_fire_at <= now()
order by s.next_fire_at, s.workload_id
limit @batch_size
for update of s, w skip locked;

-- name: RecordOccurrence :exec
update schedules
set next_fire_at = @next_fire_at,
    last_fired_at = @fired_at,
    last_task_id = sqlc.narg(task_id),
    last_error = sqlc.narg(error),
    updated_at = now()
where workload_id = @workload_id;

-- name: FunctionSchedule :one
select s.expression, s.next_fire_at, s.last_fired_at, s.last_task_id, s.last_error
from schedules s
join workloads w on w.id = s.workload_id
join apps a on a.id = w.app_id
where a.workspace_id = @workspace_id and a.name = @app_name and w.kind = 'function' and w.name = @name;

-- name: ListSchedules :many
select a.name as app_name, w.name as function_name,
       s.expression, s.next_fire_at, s.last_fired_at, s.last_task_id, s.last_error
from schedules s
join workloads w on w.id = s.workload_id
join apps a on a.id = w.app_id
where a.workspace_id = @workspace_id and (a.name, w.name) > (@after_app::text, @after_function::text)
order by a.name, w.name
limit @max_rows;

-- name: NextFireAt :many
-- When the earliest schedule fires next; no row without schedules.
select next_fire_at from schedules order by next_fire_at limit 1;
