-- name: PlatformHosts :many
-- Platform hosts that exist, with the reservations of their live
-- containers, by id after the cursor; failed ones for a day.
select h.id, h.provider, h.phase, h.state, h.capacity_state, h.last_seen_at, (h.token_hash is not null)::bool as enrolled,
       h.instance_id, h.region, h.instance_type, h.market, h.gpu_type, h.gpu_count, h.cpu_millis, h.memory_bytes,
       h.agent_version,
       coalesce(used.cpu, 0)::bigint as used_cpu, coalesce(used.memory, 0)::bigint as used_memory,
       coalesce(used.gpus, 0)::int as used_gpus, coalesce(used.containers, 0)::int as containers
from hosts h
left join lateral (
    select sum(c.cpu_millis) as cpu, sum(c.memory_bytes) as memory,
           sum(coalesce(release_gpus(r.spec), 0)) as gpus, count(*) as containers
    from containers c
    left join releases r on r.id = c.release_id
    where c.host_id = h.id and c.state <> 'stopped'
) used on true
where h.kind = 'platform' and h.phase <> 'deleted'
  and (h.phase <> 'failed' or h.phase_at > now() - interval '1 day')
  and h.id > @after_id
order by h.id
limit @max_rows;
