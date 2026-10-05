-- name: StartingContainersOnHost :many
select c.id, c.workspace_id, w.name as workspace_name, c.slots, c.cpu_millis, c.memory_bytes,
       r.spec, r.source_sha256, c.purpose, c.command, c.block_network, c.allow_list,
       r.workload_id, wl.kind as workload_kind
from containers c
join releases r on r.id = c.release_id
join workloads wl on wl.id = r.workload_id
join workspaces w on w.id = c.workspace_id
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
-- Attempts that ended without their slot knowing: the host kills them. A
-- lost attempt on a live container was stopped by request or omitted from a
-- report, and its slot must not keep running it.
select a.id, a.container_id, a.state
from containers c
join attempts a on a.container_id = c.id
where c.host_id = @host_id and c.state in ('ready', 'draining')
  and a.state in ('cancelled', 'timed_out', 'lost')
  and a.finished_at > now() - make_interval(secs => @within_seconds::float8)
order by a.id;

-- name: LiveImagesOnHost :many
-- The image references the host's live containers were started with.
select distinct image_reference::text
from containers
where host_id = @host_id and state <> 'stopped' and image_reference is not null
order by 1;

-- name: RecordImageReference :execrows
update containers set image_reference = @reference
where id = @id and host_id = @host_id and state = 'starting';
