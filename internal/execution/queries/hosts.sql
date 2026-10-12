-- name: DrainContainersOnHost :many
-- Starting and ready containers on the host stop claiming; the host stops
-- them once their running attempts finish.
update containers
set state = 'draining', drain_started_at = now()
where host_id = @host_id and state in ('starting', 'ready')
  and (sqlc.narg(workspace_ids)::uuid[] is null or workspace_id = any(sqlc.narg(workspace_ids)::uuid[]))
returning id, release_id, image_build_id;

-- name: MovableReleasesOnHosts :many
-- The releases with live serve containers on the hosts that could move to
-- Spot.
select distinct r.id
from containers c
join releases r on r.id = c.release_id
where c.host_id = any(@host_ids::uuid[]) and c.state <> 'stopped' and c.purpose = 'serve' and release_moves(r.spec);

-- name: LockContainersToEvict :many
-- The live containers among the ids not evicted yet, locked in id order.
select id from containers
where id = any(@ids::uuid[]) and state <> 'stopped' and evicted_at is null
order by id
for update;

-- name: EvictContainers :many
-- The locked containers are evicted, each with whether it runs attempts.
-- This statement's snapshot follows the locks, so it sees any attempt a
-- claim committed before them.
update containers c
set evicted_at = now()
where c.id = any(@ids::uuid[])
returning c.id, c.release_id, exists (select 1 from attempts a where a.container_id = c.id and a.state = 'running')::bool as busy;

-- name: StoppedContainersAmong :many
select id from containers where id = any(@ids::uuid[]) and state = 'stopped';
