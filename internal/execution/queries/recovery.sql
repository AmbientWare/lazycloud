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

-- name: LockLiveContainersOnHost :many
-- Locks the host's live containers in id order before host loss touches any
-- task, keeping the container, task, attempt lock order across containers.
select id from containers
where host_id = @host_id and state <> 'stopped'
order by id
for update;

-- name: StalePendingContainers :many
-- Fleet containers, in any account, pending longer than the queue timeout
-- that the fleet cannot get a host for: no offer can host them, the offers
-- that could cool or lack quota or margin, or the host bought for them has
-- not launched. In (created_at, id) order after the cursor, with why the
-- planner says they wait. Work waiting on the host limit or arrivals waits
-- on; work pinned to a joined machine waits for it.
select c.id, c.created_at, coalesce(c.capacity_wait, '')::text as wait,
       (w.connection_id is not null)::bool as connected
from containers c
join workspaces w on w.id = c.workspace_id
left join releases r on r.id = c.release_id
left join hosts h on h.id = c.capacity_host_id
where c.state = 'pending'
  and c.created_at < now() - make_interval(secs => @timeout_seconds::float8)
  and coalesce(r.spec -> 'placement' ->> 'machine', '') = ''
  and (c.capacity_wait in ('cooldown', 'quota', 'margin', 'no_offer') or c.capacity_wait = 'provisioning' and h.phase = 'requested')
  and (c.created_at, c.id) > (@after_created_at::timestamptz, @after_id::uuid)
order by c.created_at, c.id
limit @batch_size;

-- name: LockStalePendingContainer :execrows
-- Rechecks the queue timeout under the row lock: a container placed, given
-- a launched host or a wait the fleet will end after the scan keeps
-- waiting.
select c.id from containers c
left join hosts h on h.id = c.capacity_host_id
where c.id = @id and c.state = 'pending'
  and c.created_at < now() - make_interval(secs => @timeout_seconds::float8)
  and (c.capacity_wait in ('cooldown', 'quota', 'margin', 'no_offer') or c.capacity_wait = 'provisioning' and h.phase = 'requested')
for update of c;
