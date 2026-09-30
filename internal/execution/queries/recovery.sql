-- name: OverdueAttempts :many
-- Running attempts past their deadline in (deadline_at, id) order after the
-- cursor. Reads the attempts_running_deadline partial index.
select a.id, a.deadline_at, c.host_id
from attempts a
join containers c on c.id = a.container_id
where a.state = 'running'
  and a.deadline_at < now()
  and (a.deadline_at, a.id) > (@after_deadline_at::timestamptz, @after_id::uuid)
order by a.deadline_at, a.id
limit @batch_size;

-- name: StuckStartingContainers :many
-- Containers assigned longer ago than the start timeout without becoming
-- ready, in (assigned_at, id) order after the cursor.
select c.id, c.assigned_at
from containers c
where c.state = 'starting'
  and c.assigned_at < now() - make_interval(secs => @timeout_seconds::float8)
  and (c.assigned_at, c.id) > (@after_assigned_at::timestamptz, @after_id::uuid)
order by c.assigned_at, c.id
limit @batch_size;

-- name: LockStuckStartingContainer :execrows
-- Rechecks the start timeout under the row lock: a container that became
-- ready after the scan keeps running.
select id from containers
where id = @id
  and state = 'starting'
  and assigned_at < now() - make_interval(secs => @timeout_seconds::float8)
for update;
