-- name: PlatformHosts :many
-- Live platform hosts with an instance and their live containers'
-- reservations, by id after the cursor.
select h.id, h.provider, h.phase, h.state, h.capacity_state, h.last_seen_at,
       (h.token_hash is not null)::bool as enrolled, h.instance_id::text as instance_id, h.region, h.instance_type,
       h.market, h.gpu_type, h.gpu_count, h.cpu_millis, h.memory_bytes, h.agent_version, h.reserve_mode,
       h.image_evidence, h.prepared_agent_version,
       coalesce(used.cpu, 0)::bigint as used_cpu, coalesce(used.memory, 0)::bigint as used_memory,
       coalesce(used.gpus, 0)::int as used_gpus, coalesce(used.containers, 0)::int as containers
from hosts h
left join lateral (
    select sum(c.cpu_millis) as cpu, sum(c.memory_bytes) as memory,
           sum(case when c.image_build_id is null then coalesce(release_gpus(r.spec), 0) else c.gpu_count end) as gpus,
           count(*) as containers
    from containers c
    left join releases r on r.id = c.release_id
    where c.host_id = h.id and c.state <> 'stopped'
) used on true
where h.kind = 'platform' and h.instance_id is not null and h.phase not in ('deleted', 'failed')
  and h.id > @after_id
order by h.id
limit @max_rows;

-- name: FleetRollout :many
-- Live platform hosts by where they stand on the agent release: connected
-- serving or draining hosts on it (current) or on another (updating),
-- reserves, and other enrolled hosts (offline). Draining and serving match
-- fleetStateOf.
select (case
    when h.phase = 'draining' or (h.phase = 'ready' and (h.capacity_state <> 'available'
        or (h.state = 'online' and coalesce(h.last_seen_at > @live_after::timestamptz, false))))
        then case when h.agent_version = @version::text then 'current' else 'updating' end
    when h.phase in ('preparing', 'stopping', 'stopped') then 'reserve'
    when h.token_hash is not null then 'offline'
    else ''
end)::text as phase, count(*)::int as hosts
from hosts h
where h.kind = 'platform' and h.phase not in ('deleted', 'failed')
group by 1
order by 1;
