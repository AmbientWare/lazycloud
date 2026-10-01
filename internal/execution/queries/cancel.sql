-- name: LockTaskForCancel :one
select id, status, current_attempt_id, release_id
from tasks
where id = @id and workspace_id = @workspace_id
for update;

-- name: CancelTask :exec
update tasks set status = 'cancelled', finished_at = now() where id = @id;

-- name: AttemptHost :one
select c.host_id from attempts a join containers c on c.id = a.container_id where a.id = @id;
