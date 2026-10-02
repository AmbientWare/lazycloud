-- name: ContainerAuthority :one
-- The container and its workspace, when it is assigned to the host.
select c.state, c.workspace_id, w.name as workspace_name, w.state as workspace_state
from containers c
join workspaces w on w.id = c.workspace_id
where c.id = @id and c.host_id = @host_id;

-- name: RunningTaskOnContainer :one
-- The task's current attempt, when it runs on the container.
select a.id as attempt_id, coalesce(t.root_task_id, t.id)::uuid as root_task_id
from tasks t
join attempts a on a.id = t.current_attempt_id
where t.id = @task_id and t.status = 'running' and a.container_id = @container_id and a.state = 'running';

-- name: LockStartingContainer :one
select state from containers where id = @id and host_id = @host_id for update;

-- name: EnqueueCallbacks :execrows
-- One outbox row per task whose release names a callback_url. A repeated
-- transition for the same attempt adds nothing.
insert into task_callbacks (task_id, workspace_id, url, event, attempt, max_attempts, failure)
select t.id, t.workspace_id, r.spec ->> 'callback_url', @event, t.attempt_count, t.max_attempts, sqlc.narg(failure)::jsonb
from tasks t
join releases r on r.id = t.release_id
where t.id = any(@task_ids::uuid[]) and r.spec ->> 'callback_url' is not null
on conflict (task_id, event, attempt) do nothing;

-- name: WakeCallbackDelivery :exec
-- Delivered when the transaction commits; the deliverer runs at once
-- instead of on its next tick. The channel is database.ChannelCallback.
select pg_notify('lc_callback', '');
