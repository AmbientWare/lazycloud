-- name: PlatformHosts :many
-- Live platform hosts with an instance and their live containers'
-- reservations, by id after the cursor.
select h.id, h.provider, h.phase, h.state, h.capacity_state, h.last_seen_at,
       (h.token_hash is not null)::bool as enrolled, h.instance_id::text as instance_id, h.region, h.instance_type,
       h.market, h.gpu_type, h.gpu_count, h.cpu_millis, h.memory_bytes, h.agent_version, h.reserve_mode,
       h.image_evidence, on_target(h.rollout_bucket, h.agent_version)::bool as agent_current,
       on_target(h.rollout_bucket, h.prepared_agent_version)::bool as prepared_current,
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
-- serving or draining hosts that run what they should (current) or not
-- (updating),
-- reserves, and other enrolled hosts (offline). Draining and serving match
-- fleetStateOf.
select (case
    when h.phase = 'draining' or (h.phase = 'ready' and (h.capacity_state <> 'available'
        or (h.state = 'online' and coalesce(h.last_seen_at > @live_after::timestamptz, false))))
        then case when on_target(h.rollout_bucket, h.agent_version) then 'current' else 'updating' end
    when h.phase in ('preparing', 'stopping', 'stopped') then 'reserve'
    when h.token_hash is not null then 'offline'
    else ''
end)::text as phase, count(*)::int as hosts
from hosts h
where h.kind = 'platform' and h.phase not in ('deleted', 'failed')
group by 1
order by 1;

-- name: PlatformHostSpend :many
-- Each platform cloud host live now or since @since: what prices it, its
-- phase now, and the seconds since @since it ran (a billed phase, as in
-- AtLossSpentToday) and lay stopped, from its phase history.
with spans as (
    select c.host_id, c.phase, c.at,
           lead(c.at, 1, now()) over (partition by c.host_id order by c.at) as until
    from host_phase_changes c
    join hosts h on h.id = c.host_id
    where h.kind = 'platform' and h.provider = 'aws'
      and (h.phase not in ('deleted', 'failed') or h.phase_at > @since::timestamptz)
)
select h.id, h.instance_type, h.region, h.hibernation_configured, h.phase,
       coalesce(h.hourly_micros, 0)::bigint as hourly_micros,
       coalesce(sum(extract(epoch from s.until - greatest(s.at, @since::timestamptz))) filter (where s.phase in
           ('provisioning', 'booting', 'joining', 'ready', 'draining', 'preparing', 'stopping', 'resuming')), 0)::float8 as running_seconds,
       coalesce(sum(extract(epoch from s.until - greatest(s.at, @since::timestamptz))) filter (where s.phase = 'stopped'), 0)::float8 as stopped_seconds
from hosts h
join spans s on s.host_id = h.id
where s.until > @since::timestamptz
group by h.id
order by h.id;
