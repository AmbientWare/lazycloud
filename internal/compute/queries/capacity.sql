-- name: AvailableCapacity :many
-- Online, recently seen hosts with their capacity minus the resources of
-- their live containers.
select h.id,
       h.cpu_millis,
       h.memory_bytes,
       (h.cpu_millis - coalesce(sum(c.cpu_millis), 0))::bigint as free_cpu_millis,
       (h.memory_bytes - coalesce(sum(c.memory_bytes), 0))::bigint as free_memory_bytes
from hosts h
left join containers c on c.host_id = h.id and c.state <> 'stopped'
where h.state = 'online'
  and h.last_seen_at >= now() - make_interval(secs => @timeout_seconds::float8)
group by h.id
order by h.id;
