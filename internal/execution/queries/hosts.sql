-- name: DrainContainersOnHost :many
-- Starting and ready containers on the host stop claiming; the host stops
-- them once their running attempts finish.
update containers
set state = 'draining', drain_started_at = now()
where host_id = @host_id and state in ('starting', 'ready')
  and (sqlc.narg(workspace_ids)::uuid[] is null or workspace_id = any(sqlc.narg(workspace_ids)::uuid[]))
returning id, release_id, image_build_id;

-- name: SpotTolerantReleasesOnHosts :many
-- The releases with live Spot-tolerant serve containers on the hosts.
select distinct c.release_id::uuid as release_id
from containers c
where c.host_id = any(@host_ids::uuid[]) and c.state <> 'stopped' and c.purpose = 'serve'
  and c.release_id is not null and c.rate_class in ('auto', 'pinned');

-- name: StoppedContainersAmong :many
select id from containers where id = any(@ids::uuid[]) and state = 'stopped';
