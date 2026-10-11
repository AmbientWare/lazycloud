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

-- name: StoppedContainersAmong :many
select id from containers where id = any(@ids::uuid[]) and state = 'stopped';
