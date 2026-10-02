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
-- containers reserve and how many of them did not accept interruption.
select h.id, h.kind, h.connection_id, h.phase, h.phase_at, h.state, h.capacity_state, h.capacity_reason,
       h.last_seen_at, h.session_epoch, h.region, h.availability_zone, h.availability_zone_id, h.instance_type,
       h.market, h.gpu_type, h.gpu_count, h.cpu_millis, h.memory_bytes, h.hourly_micros, h.launched_at,
       h.interruption_at, h.reserve_mode, h.hibernation_configured, h.spot_request_id, h.image_evidence,
       h.prepared_agent_version, h.updating_until, h.light_since,
       coalesce(used.cpu, 0)::bigint as used_cpu, coalesce(used.memory, 0)::bigint as used_memory,
       coalesce(used.gpus, 0)::int as used_gpus, coalesce(used.containers, 0)::int as containers,
       coalesce(used.pinned, 0)::int as pinned
from hosts h
left join lateral (
    select sum(c.cpu_millis) as cpu, sum(c.memory_bytes) as memory,
           sum(case when c.image_build_id is null then coalesce(release_gpus(r.spec), 0) else c.gpu_count end) as gpus,
           count(*) as containers,
           count(*) filter (where c.rate_class in ('non_preemptible', 'pinned_non_preemptible')) as pinned
    from containers c
    left join releases r on r.id = c.release_id
    where c.host_id = h.id and c.state <> 'stopped'
) used on true
where h.provider = 'aws' and h.phase not in ('deleted', 'failed')
order by h.id;

-- name: PendingDemand :many
-- The oldest pending containers, up to the batch, grouped by what they need
-- from a host, each with the host bought for it (the zero id for none).
with batch as (
    select c.id, c.workspace_id, c.release_id, c.image_build_id, c.cpu_millis, c.memory_bytes, c.capacity_host_id
    from containers c
    where c.state = 'pending'
    order by c.created_at, c.id
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

-- name: RecentArrivals :many
-- Platform containers created since @since_id, a uuidv7 bound on the
-- primary key, by 10-second bucket and shape: how many arrived, excluding
-- those still pending, and how long the stopped ones served. gpu_type is
-- the model placement gave them.
select date_bin('10 seconds', c.created_at, timestamptz '2000-01-01 00:00:00+00')::timestamptz as at,
       coalesce((r.spec -> 'placement' ->> 'preemptible')::boolean, true)::bool as preemptible,
       coalesce(r.spec -> 'resources' -> 'gpu', build_gpus(c.image_build_id), '[]'::jsonb)::jsonb as gpus,
       c.gpu_type, c.cpu_millis, c.memory_bytes, c.gpu_count,
       coalesce(r.spec -> 'placement' ->> 'region', '')::text as region,
       coalesce(r.spec -> 'placement' ->> 'availability_zone', '')::text as zone,
       count(*) filter (where c.state <> 'pending')::int as arrived,
       coalesce(avg(extract(epoch from c.stopped_at - c.ready_at))
           filter (where c.stopped_at is not null and c.ready_at is not null), 0)::float8 as served_seconds
from containers c
join workspaces ws on ws.id = c.workspace_id
left join releases r on r.id = c.release_id
where c.id >= @since_id and ws.connection_id is null
  and coalesce(r.spec -> 'placement' ->> 'machine', '') = ''
group by 1, 2, 3, 4, 5, 6, 7, 8, 9
order by 1;

-- name: ScheduledDemand :many
-- Platform functions due to fire by @until, with their active release's
-- shape and scaling, their live containers and the 95th percentile run
-- time of tasks since @since_id.
select s.workload_id, s.next_fire_at, (r.spec -> 'resources' ->> 'cpu_millis')::bigint as cpu_millis,
       ((r.spec -> 'resources' ->> 'memory_mib')::bigint * 1048576)::bigint as memory_bytes,
       coalesce(r.spec -> 'resources' -> 'gpu', '[]'::jsonb)::jsonb as gpus,
       coalesce((r.spec -> 'resources' ->> 'gpu_count')::int, 0)::int as gpu_count,
       coalesce((r.spec -> 'placement' ->> 'preemptible')::boolean, true)::bool as preemptible,
       coalesce(r.spec -> 'placement' ->> 'region', '')::text as region,
       coalesce(r.spec -> 'placement' ->> 'availability_zone', '')::text as zone,
       coalesce((r.spec ->> 'concurrency')::int, 1)::int as concurrency,
       coalesce((r.spec -> 'autoscaler' ->> 'max_containers')::int, 1)::int as max_containers,
       coalesce((r.spec -> 'autoscaler' ->> 'min_containers')::int, 0)::int as min_containers,
       coalesce((r.spec ->> 'keep_warm_seconds')::int, 10)::int as keep_warm_seconds,
       coalesce(live.containers, 0)::int as live_containers,
       coalesce(run.p95, 0)::float8 as run_seconds
from schedules s
join workloads w on w.id = s.workload_id
join releases r on r.id = w.active_release_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
left join lateral (
    select count(*) as containers from containers c where c.release_id = r.id and c.state <> 'stopped'
) live on true
left join lateral (
    select percentile_cont(0.95) within group (order by extract(epoch from t.finished_at - t.started_at)) as p95
    from tasks t
    where t.workload_id = w.id and t.id >= @since_id and t.finished_at > t.started_at
) run on true
where s.next_fire_at > now() and s.next_fire_at <= @until
  and w.desired_state = 'active' and a.state = 'active' and ws.state = 'active' and ws.connection_id is null
  and coalesce(r.spec -> 'placement' ->> 'machine', '') = ''
  and coalesce(jsonb_array_length(r.spec -> 'disks'), 0) = 0
order by s.next_fire_at, s.workload_id;

-- name: ActivationStats :many
-- Fleet activations of the last day by kind, hardware and resume outcome,
-- with the 95th percentile time of those that became ready.
select kind, instance_type, region, gpu_type,
       (case when outcome in ('memory_restored', 'cold_boot') then outcome else '' end)::text as resume_outcome,
       count(*) filter (where outcome <> 'failed')::int as ready,
       count(*) filter (where outcome = 'failed')::int as failed,
       coalesce(percentile_cont(0.95) within group (order by seconds) filter (where outcome <> 'failed'), 0)::float8 as p95
from fleet_activations
where at > now() - interval '1 day'
group by 1, 2, 3, 4, 5
order by 1, 2, 3, 4, 5;

-- name: PlannerCooldowns :many
-- Offers cooling now, and refusals recent enough to cool their region. A
-- cooldown with no refusal behind it has no refused_at.
select connection_key, region, instance_type, market, until, refused_at
from capacity_cooldowns
where until > now() or refused_at > now() - make_interval(secs => @window_seconds::float8)
order by connection_key, region, instance_type, market;

-- name: FleetMarkets :many
-- The published markets; current is false once a plan expired.
select market, plan, generated_at, expires_at, (expires_at > now())::bool as current, pressure_since,
       consolidating_host, consolidation_started_at, consolidation_cooldown_until
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
    reserve_mode = case when v.refresh then h.reserve_mode end, light_since = null, updated_at = now()
from (select unnest(@ids::uuid[]) as id, unnest(@refresh::bool[]) as refresh) v
where h.id = v.id and h.phase = 'stopped'
returning h.id;

-- name: ReturnToReserve :many
-- Idle serving hosts start proving they may stop into the reserve; a host
-- that took a container since the snapshot stays.
update hosts h
set phase = 'preparing', phase_message = 'Preparing to stop into the reserve', phase_at = now(), reserve_mode = v.mode,
    light_since = null, updated_at = now()
from (select unnest(@ids::uuid[]) as id, unnest(@modes::text[]) as mode) v
where h.id = v.id and h.phase = 'ready' and h.capacity_state = 'available'
  and not exists (select 1 from containers c where c.host_id = h.id and c.state <> 'stopped')
returning h.id;

-- name: DrainHosts :many
-- Idle serving hosts, and reserves still preparing, drain toward
-- termination; a host that took a container since the snapshot stays.
update hosts h
set phase = 'draining', capacity_state = 'draining', capacity_reason = @reason, reserve_mode = null,
    light_since = null, phase_message = 'Draining; no new work is placed here', phase_at = now(), updated_at = now()
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

-- name: CordonHosts :many
-- Lightly used hosts stop taking work while their containers drain onto
-- the rest of the market.
update hosts
set capacity_state = 'draining', capacity_reason = 'consolidating', updated_at = now()
where id = any(@ids::uuid[]) and phase = 'ready' and capacity_state = 'available'
returning id;

-- name: UncordonHosts :exec
-- A consolidated host that emptied takes work again; retention decides
-- what happens to it.
update hosts h
set capacity_state = 'available', capacity_reason = '', updated_at = now()
where h.id = any(@ids::uuid[]) and h.phase = 'ready' and h.capacity_reason = 'consolidating'
  and not exists (select 1 from containers c where c.host_id = h.id and c.state <> 'stopped');

-- name: SetLightSince :exec
-- When each changed serving host became lightly used; null clears it.
update hosts h
set light_since = v.light_since
from jsonb_to_recordset(@hosts::jsonb) as v(id uuid, light_since timestamptz)
where h.id = v.id and h.light_since is distinct from v.light_since;

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
-- Each changed market's published plan and planner state. Rows carry the
-- stored plan forward when this pass publishes none.
insert into fleet_markets (market, plan, generated_at, expires_at, pressure_since, consolidating_host,
                           consolidation_started_at, consolidation_cooldown_until)
select v.market, v.plan, v.generated_at, v.expires_at, v.pressure_since, v.consolidating_host,
       v.consolidation_started_at, v.consolidation_cooldown_until
from jsonb_to_recordset(@markets::jsonb) as v(
    market text, plan jsonb, generated_at timestamptz, expires_at timestamptz, pressure_since timestamptz,
    consolidating_host uuid, consolidation_started_at timestamptz, consolidation_cooldown_until timestamptz)
on conflict (market) do update
set plan = excluded.plan, generated_at = excluded.generated_at, expires_at = excluded.expires_at,
    pressure_since = excluded.pressure_since, consolidating_host = excluded.consolidating_host,
    consolidation_started_at = excluded.consolidation_started_at,
    consolidation_cooldown_until = excluded.consolidation_cooldown_until;

-- name: NotifyHosts :exec
-- Wakes the sessions of hosts the pass moved, once it commits.
select pg_notify(@channel::text, v.id) from unnest(@ids::text[]) as v(id);
