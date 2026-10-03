-- The fleet planning pass: one snapshot read under the capacity lock, then
-- the intents it decides, written in the same transaction. Each statement
-- reads live or recent rows through an index, whatever the backlog or
-- history.

-- name: LockFleet :one
-- One planner decides at a time, so two passes never buy for the same
-- shortfall. now is the transaction's clock, which every write uses.
select pg_try_advisory_xact_lock(hashtextextended('capacity', 0))::bool as locked, now()::timestamptz as now;

-- name: PlannerHosts :many
-- Every cloud host the fleet holds or is buying, with what its live
-- containers reserve.
select h.id, h.kind, h.connection_id, h.phase, h.phase_at, h.state, h.capacity_state,
       h.last_seen_at, h.session_epoch, h.region, h.availability_zone, h.availability_zone_id, h.instance_type,
       h.market, h.gpu_type, h.gpu_count, h.cpu_millis, h.memory_bytes, h.hourly_micros,
       h.interruption_at, h.reserve_mode, h.hibernation_configured, h.spot_request_id, h.image_evidence,
       h.prepared_agent_version, h.updating_until, h.idle_since,
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
where h.provider = 'aws' and h.phase not in ('deleted', 'failed')
order by h.id;

-- name: PendingDemand :many
-- The oldest pending containers, up to the batch, grouped by what they need
-- from a host, each with the host bought for it (the zero id for none). The
-- batch follows the pending index alone: ordering ties by id would sort a
-- whole backlog created in one statement.
with batch as (
    select c.id, c.workspace_id, c.release_id, c.image_build_id, c.cpu_millis, c.memory_bytes, c.capacity_host_id
    from containers c
    where c.state = 'pending'
    order by c.created_at
    limit @batch_size
)
select ws.connection_id, b.cpu_millis, b.memory_bytes,
       coalesce(r.spec -> 'placement' ->> 'machine', '')::text as machine,
       coalesce(r.spec -> 'placement' ->> 'region', '')::text as region,
       coalesce(r.spec -> 'placement' ->> 'availability_zone', '')::text as zone,
       coalesce((r.spec -> 'placement' ->> 'preemptible')::boolean, true)::bool as preemptible,
       coalesce(r.spec -> 'resources' -> 'gpu', build_gpus(b.image_build_id), '[]'::jsonb)::jsonb as gpus,
       coalesce((r.spec -> 'resources' ->> 'gpu_count')::int, 0)::int as gpu_count,
       array_agg(b.id order by b.id)::uuid[] as ids,
       array_agg(coalesce(b.capacity_host_id, '00000000-0000-0000-0000-000000000000'::uuid) order by b.id)::uuid[]
           as bought
from batch b
join workspaces ws on ws.id = b.workspace_id
left join releases r on r.id = b.release_id
group by 1, 2, 3, 4, 5, 6, 7, 8, 9
order by 4, 5, 6, 2 desc, 3 desc;

-- name: PlannerCooldowns :many
-- Offers cooling now, and refusals recent enough to cool their region. A
-- cooldown with no refusal behind it has no refused_at.
select connection_key, region, instance_type, market, until, refused_at
from capacity_cooldowns
where until > now() or refused_at > now() - make_interval(secs => @window_seconds::float8)
order by connection_key, region, instance_type, market;

-- name: FleetMarkets :many
-- The published markets. An expired plan is no plan; readers compare
-- expires_at with their clock. The cast gives sqlc a row type apart from
-- the admin FleetMarket.
select market::text as market, plan, generated_at, expires_at
from fleet_markets
order by market;

-- name: HostingConnections :many
-- Connections that take new workloads, with the networks of their active
-- authorization.
select cc.id, a.networks
from cloud_connections cc
join cloud_authorizations a on a.connection_id = cc.id and a.slot = 'active' and a.phase = 'ready'
where cc.phase in ('ready', 'reconnect_pending', 'retiring_authorization')
order by cc.id;

-- name: InsertRequestedHosts :exec
-- Hosts to buy, for the launcher. The planner names each id, so container
-- waits can point at a host bought in the same statement.
insert into hosts (id, name, state, kind, provider, connection_id, phase, phase_message, cpu_millis, memory_bytes,
                   gpu_type, gpu_count, region, availability_zone, availability_zone_id, instance_type, market,
                   hourly_micros, reserve_mode)
select v.id, 'lazycloud-' || v.instance_type, 'offline', v.kind, 'aws', v.connection_id, 'requested',
       'Waiting for the machine to be launched', v.cpu_millis, v.memory_bytes, v.gpu_type, v.gpu_count, v.region,
       v.availability_zone, v.availability_zone_id, v.instance_type, v.market, v.hourly_micros, v.reserve_mode
from jsonb_to_recordset(@hosts::jsonb) as v(
    id uuid, kind text, connection_id uuid, cpu_millis bigint, memory_bytes bigint, gpu_type text, gpu_count integer,
    region text, availability_zone text, availability_zone_id text, instance_type text, market text,
    hourly_micros bigint, reserve_mode text);

-- name: ResumeReserves :many
-- Starts stopped reserves: to serve, the reserve mode cleared, or to
-- refresh, kept so the host prepares and stops again once it joins. Only a
-- requested resume serves.
update hosts h
set phase = 'resuming', phase_message = 'Starting from the reserve', phase_at = now(), resume_requested_at = now(),
    reserve_mode = case when v.refresh then h.reserve_mode end, idle_since = null, updated_at = now()
from (select unnest(@ids::uuid[]) as id, unnest(@refresh::bool[]) as refresh) v
where h.id = v.id and h.phase = 'stopped'
returning h.id;

-- name: ReturnToReserve :many
-- Idle serving hosts start proving they may stop into the reserve, each in
-- the mode the planner chose; a host that took a container since the
-- snapshot stays.
update hosts h
set phase = 'preparing', phase_message = 'Preparing to stop into the reserve', phase_at = now(),
    reserve_mode = case when h.id = any(@hibernate_ids::uuid[]) then 'hibernate' else 'stop' end,
    idle_since = null, updated_at = now()
where h.id = any(@ids::uuid[]) and h.phase = 'ready' and h.capacity_state = 'available'
  and not exists (select 1 from containers c where c.host_id = h.id and c.state <> 'stopped')
returning h.id;

-- name: DrainHosts :many
-- Idle serving hosts, and reserves still preparing, drain toward
-- termination; a host that took a container since the snapshot stays.
update hosts h
set phase = 'draining', capacity_state = 'draining', capacity_reason = @reason, reserve_mode = null,
    idle_since = null, phase_message = 'Draining; no new work is placed here', phase_at = now(), updated_at = now()
where h.id = any(@ids::uuid[]) and h.phase in ('ready', 'preparing')
  and not exists (select 1 from containers c where c.host_id = h.id and c.state <> 'stopped')
returning h.id;

-- name: RetireReserves :many
-- Stopped reserves terminate; a reserve not launched yet is removed.
update hosts h
set phase = case when h.phase = 'requested' then 'deleted' else 'terminating' end,
    phase_message = case when h.phase = 'requested' then 'Removed' else 'Shutting down' end,
    phase_at = now(), state = 'retired', token_hash = null, updated_at = now()
where h.id = any(@ids::uuid[])
  and (h.phase = 'stopped'
       or (h.phase = 'requested' and h.reserve_mode is not null
           and (h.launch_lease_until is null or h.launch_lease_until < now())))
returning h.id;

-- name: SetIdleSince :exec
-- When each changed serving host became idle; null clears it.
update hosts h
set idle_since = v.idle_since
from jsonb_to_recordset(@hosts::jsonb) as v(id uuid, idle_since timestamptz)
where h.id = v.id and h.idle_since is distinct from v.idle_since;

-- name: SetCapacityWaits :execrows
-- Records why each pending container waits; unchanged rows are not
-- written.
update containers c
set capacity_wait = nullif(w.wait, ''), capacity_host_id = nullif(w.host, '00000000-0000-0000-0000-000000000000'::uuid)
from (select unnest(@ids::uuid[]) as id, unnest(@waits::text[]) as wait, unnest(@hosts::uuid[]) as host) w
where c.id = w.id and c.state = 'pending'
  and (c.capacity_wait is distinct from nullif(w.wait, '')
       or c.capacity_host_id is distinct from nullif(w.host, '00000000-0000-0000-0000-000000000000'::uuid));

-- name: CoolOffers :exec
-- Offers whose bought host joined and still could not take its container
-- cool without counting as a provider refusal.
insert into capacity_cooldowns (connection_key, region, instance_type, market, until, reason)
select v.connection_key, v.region, v.instance_type, v.market, now() + make_interval(secs => @seconds::float8),
       'the host bought for a container could not take it'
from jsonb_to_recordset(@offers::jsonb) as v(connection_key text, region text, instance_type text, market text)
on conflict (connection_key, region, instance_type, market) do update set until = excluded.until, reason = excluded.reason;

-- name: FailPreparing :many
-- Reserves whose agent never proved them within the limit fail, and
-- reconcile terminates their instances. An agent update in flight waits.
update hosts
set phase = 'failed', failure = 'service_lost', phase_message = @message, phase_at = now(), state = 'retired',
    token_hash = null, launch_lease_until = null, updated_at = now()
where id = any(@ids::uuid[]) and phase = 'preparing' and phase_at < now() - make_interval(secs => @seconds::float8)
  and (updating_until is null or updating_until < now())
returning id;

-- name: UpsertFleetMarkets :exec
-- Every platform market's published plan.
insert into fleet_markets (market, plan, generated_at, expires_at)
select v.market, v.plan, v.generated_at, v.expires_at
from jsonb_to_recordset(@markets::jsonb) as v(market text, plan jsonb, generated_at timestamptz, expires_at timestamptz)
on conflict (market) do update
set plan = excluded.plan, generated_at = excluded.generated_at, expires_at = excluded.expires_at;

-- name: NotifyHosts :exec
-- Wakes the sessions of hosts the pass moved, once it commits.
select pg_notify(@channel::text, v.id) from unnest(@ids::text[]) as v(id);
