-- name: StartingContainersOnHost :many
select c.id, c.workspace_id, w.name as workspace_name, c.slots, c.cpu_millis, c.memory_bytes,
       r.spec, r.source_sha256
from containers c
join releases r on r.id = c.release_id
join workspaces w on w.id = c.workspace_id
where c.host_id = @host_id and c.state = 'starting'
order by c.id;

-- name: IdleDrainingContainersOnHost :many
select c.id
from containers c
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
