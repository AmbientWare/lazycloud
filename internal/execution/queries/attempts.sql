-- name: LockTaskForAttempt :one
-- Lock order everywhere in execution: container, then task, then attempt.
select t.id, t.status, t.attempt_count, t.max_attempts, t.release_id,
       t.current_attempt_id, r.spec
from tasks t
join releases r on r.id = t.release_id
where t.id = (select task_id from attempts where attempts.id = @attempt_id)
for update of t;

-- name: LockRunningAttempt :one
select a.id, a.task_id, a.number, a.container_id, c.host_id
from attempts a
join containers c on c.id = a.container_id
where a.id = @attempt_id and a.state = 'running'
for update of a;

-- name: SetAttemptState :exec
update attempts set state = @state, finished_at = now() where id = @id;

-- name: SucceedTask :exec
update tasks set status = 'succeeded', finished_at = now() where id = @id;

-- name: InsertTaskResult :exec
insert into task_results (task_id, encoding, data) values (@task_id, @encoding, @data);

-- name: RequeueTask :exec
update tasks
set status = 'queued',
    current_attempt_id = null,
    available_at = now() + make_interval(secs => @delay_seconds::float8)
where id = @id;

-- name: FailTask :exec
update tasks set status = 'failed', failure = @failure, finished_at = now() where id = @id;
