-- name: AvailableCapacity :many
-- Hosts that take new containers, with their capacity minus the resources of
-- their live containers. Reads the live-container partial index per host.
select h.id,
       h.kind,
       h.provider,
       h.connection_id,
       h.name,
       h.region,
       h.availability_zone,
       h.availability_zone_id as zone_id,
       h.market,
       h.gpu_type,
       h.gpu_count,
       h.cpu_millis,
       h.memory_bytes,
       (h.cpu_millis - coalesce(used.cpu, 0))::bigint as free_cpu_millis,
       (h.memory_bytes - coalesce(used.memory, 0))::bigint as free_memory_bytes,
       (h.gpu_count - coalesce(used.gpus, 0))::int as free_gpus,
       array(select hw.workspace_id from host_workspaces hw where hw.host_id = h.id order by hw.workspace_id)::uuid[] as workspaces
from hosts h
left join lateral (
    select sum(c.cpu_millis) as cpu, sum(c.memory_bytes) as memory,
           sum(coalesce(release_gpus(r.spec), 0)) as gpus
    from containers c
    left join releases r on r.id = c.release_id
    where c.host_id = h.id and c.state <> 'stopped'
) used on true
where h.state = 'online'
  and h.phase = 'ready'
  and h.capacity_state = 'available'
  and h.last_seen_at >= now() - make_interval(secs => @timeout_seconds::float8)
order by h.id;
