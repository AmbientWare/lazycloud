-- name: StartingContainersOnHost :many
select c.id, c.workspace_id, c.slots, c.cpu_millis, c.memory_bytes, r.spec, r.source_sha256
from containers c
join releases r on r.id = c.release_id
where c.host_id = @host_id and c.state = 'starting'
order by c.id;

-- name: IdleDrainingContainersOnHost :many
-- An HTTP container has no attempts; its supervisor finishes the requests in
-- flight, which may take the release's timeout.
select c.id,
       (case when coalesce(r.spec ? 'http', false) then coalesce((r.spec ->> 'timeout_seconds')::int, 0) else 0 end)::int as grace_seconds
from containers c
left join releases r on r.id = c.release_id
where c.host_id = @host_id and c.state = 'draining'
  and not exists (select 1 from attempts a where a.container_id = c.id and a.state = 'running')
order by c.id;

-- name: EndedAttemptsOnHost :many
-- Attempts that ended without their slot knowing: the host kills them.
select a.id, a.container_id, a.state
from containers c
join attempts a on a.container_id = c.id
where c.host_id = @host_id and c.state in ('ready', 'draining')
  and a.state in ('cancelled', 'timed_out')
  and a.finished_at > now() - make_interval(secs => @within_seconds::float8)
order by a.id;
