-- name: LockTasksForAttempts :many
-- Lock order everywhere in execution: container, then task, then attempt.
-- Tasks lock in id order so concurrent batches never deadlock on each other.
select a.id as attempt_id, t.id, t.status, t.attempt_count, t.max_attempts, t.release_id,
       t.current_attempt_id
from attempts a
join tasks t on t.id = a.task_id
where a.id = any(@attempt_ids::uuid[])
order by t.id
for update of t;

-- name: LockRunningAttempts :many
-- Running attempts in id order, with the container each runs on.
select a.id, a.container_id, c.host_id, c.state as container_state
from attempts a
join containers c on c.id = a.container_id
where a.id = any(@ids::uuid[]) and a.state = 'running'
order by a.id
for update of a;

-- name: SetAttemptStates :exec
update attempts a
set state = v.state, finished_at = now()
from (select unnest(@ids::uuid[]) as id, unnest(@states::text[]) as state) v
where a.id = v.id;

-- name: ReleaseSpecs :many
select id, spec from releases where id = any(@ids::uuid[]);

-- name: SucceedTasks :exec
update tasks set status = 'succeeded', finished_at = now() where id = any(@ids::uuid[]);

-- name: InsertTaskResults :exec
-- A null display element stores no display.
insert into task_results (task_id, encoding, data, display)
select unnest(@task_ids::uuid[]), unnest(@encodings::text[]), unnest(@data::bytea[]), unnest(@displays::jsonb[]);

-- name: RequeueTasks :exec
update tasks t
set status = 'queued',
    current_attempt_id = null,
    available_at = now() + make_interval(secs => v.delay_seconds)
from (select unnest(@ids::uuid[]) as id, unnest(@delay_seconds::float8[]) as delay_seconds) v
where t.id = v.id;

-- name: FailRunningTasks :exec
update tasks t
set status = 'failed', failure = v.failure, finished_at = now()
from (select unnest(@ids::uuid[]) as id, unnest(@failures::jsonb[]) as failure) v
where t.id = v.id;
