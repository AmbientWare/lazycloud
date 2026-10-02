-- name: LockTaskForCancel :one
-- Without a workspace the caller already knows the task, as planning does.
select id, status, current_attempt_id, release_id
from tasks
where id = @id and (sqlc.narg(workspace_id)::uuid is null or workspace_id = sqlc.narg(workspace_id)::uuid)
for update;

-- name: CancelTask :exec
update tasks set status = 'cancelled', finished_at = now() where id = @id;

-- name: AttemptHost :one
select c.host_id from attempts a join containers c on c.id = a.container_id where a.id = @id;
