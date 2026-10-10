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
-- containers reserve, and what those that could run on Spot reserve.
select h.id, h.kind, h.connection_id, h.phase, h.phase_at, h.state, h.capacity_state,
       h.last_seen_at, h.session_epoch, h.region, h.availability_zone, h.availability_zone_id, h.instance_type,
       h.market, h.gpu_type, h.gpu_count, h.cpu_millis, h.memory_bytes, h.hourly_micros,
       h.interruption_at, h.reserve_mode, h.hibernation_configured, h.spot_request_id, h.image_evidence,
       h.prepared_agent_version, h.updating_until, h.idle_since, h.replaces,
       h.rightsize_refused_at,
       coalesce(used.cpu, 0)::bigint as used_cpu, coalesce(used.memory, 0)::bigint as used_memory,
       coalesce(used.gpus, 0)::int as used_gpus, coalesce(used.containers, 0)::int as containers,
       coalesce(used.tolerant_cpu, 0)::bigint as tolerant_cpu, coalesce(used.tolerant_memory, 0)::bigint as tolerant_memory,
       coalesce(used.busy_since, h.phase_at)::timestamptz as busy_since
from hosts h
left join lateral (
    select sum(c.cpu_millis) as cpu, sum(c.memory_bytes) as memory,
           sum(c.cpu_millis) filter (where c.rate_class in ('auto', 'pinned')) as tolerant_cpu,
           sum(c.memory_bytes) filter (where c.rate_class in ('auto', 'pinned')) as tolerant_memory,
           sum(case when c.image_build_id is null then coalesce(release_gpus(r.spec), 0) else c.gpu_count end) as gpus,
           count(*) as containers, max(c.assigned_at) as busy_since
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
-- whole backlog created in one statement. A mirror build runs on the
-- platform, as placement puts it, whatever its workspace's connection.
with batch as (
    select c.id, c.workspace_id, c.release_id, c.image_build_id, c.cpu_millis, c.memory_bytes, c.capacity_host_id, c.traceparent
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
           as bought,
       array_agg(coalesce(b.traceparent, '') order by b.id)::text[] as traceparents
from batch b
left join image_builds ib on ib.id = b.image_build_id
left join workspaces ws on ws.id = b.workspace_id and ib.mirror is not true
left join releases r on r.id = b.release_id
group by 1, 2, 3, 4, 5, 6, 7, 8, 9
order by 4, 5, 6, 2 desc, 3 desc;

-- name: RecentShapes :many
-- By purchase market and GPU model: the largest CPU, memory and GPUs placed
-- platform containers reserved among the newest containers created within
-- the window, up to the sample (recent); the same among the build
-- containers that stopped within the build window (build), since a running
-- build's host holds its slot once it ends; and what the placed
-- Spot-tolerant platform CPU containers of the sample created within the
-- arrival window reserve, less their largest batch (arrived): a steady rate
-- of work, which a single burst is not. A batch is arrivals less than
-- batch_quiet apart, at most batch_max long, as BatchWaits forms them. The
-- builds read are those created within build_scan, the window and the
-- longest a build runs. Both reads follow primary keys, so they read at
-- most sample_size containers and the scan's builds whatever the history;
-- the subqueries make their bounds constants the indexes can use.
with recent as (
    select c.id, c.created_at, c.cpu_millis, c.memory_bytes, c.gpu_count, c.gpu_type, c.rate_class, c.billing_owner, c.assigned_at
    from containers c
    where c.id > (select uuidv7(- make_interval(secs => @window_seconds::float8)))
    order by c.id desc
    limit @sample_size
), builds as (
    select c.cpu_millis, c.memory_bytes, c.gpu_count, c.gpu_type, c.rate_class
    from image_builds b
    join containers c on c.image_build_id = b.id
    where b.id > (select uuidv7(- make_interval(secs => @build_scan_seconds::float8)))
      and c.billing_owner = 'platform_fleet' and c.assigned_at is not null and c.state = 'stopped'
      and c.stopped_at > now() - make_interval(secs => @build_window_seconds::float8)
)
select 'recent'::text as kind, (rate_class in ('auto', 'pinned'))::bool as preemptible, gpu_type,
       max(cpu_millis)::bigint as cpu_millis, max(memory_bytes)::bigint as memory_bytes, max(gpu_count)::int as gpus
from recent
where billing_owner = 'platform_fleet' and assigned_at is not null
group by 2, 3
union all
select 'build', (rate_class in ('auto', 'pinned'))::bool, gpu_type,
       max(cpu_millis)::bigint, max(memory_bytes)::bigint, max(gpu_count)::int
from builds
group by 2, 3
union all
select 'arrived', true, '', (sum(cpu) - max(cpu))::bigint, (sum(memory) - max(memory))::bigint, 0
from (
    select sum(cpu_millis) as cpu, sum(memory_bytes) as memory
    from (
        select cpu_millis, memory_bytes, run,
               floor(extract(epoch from created_at - min(created_at) over (partition by run)) / @batch_max_seconds::float8) as part
        from (
            select cpu_millis, memory_bytes, created_at, count(*) filter (where opens) over (order by created_at, id) as run
            from (
                select id, cpu_millis, memory_bytes, created_at,
                       coalesce(created_at - lag(created_at) over (order by created_at, id), interval '1 day')
                           >= make_interval(secs => @batch_quiet_seconds::float8) as opens
                from recent
                where billing_owner = 'platform_fleet' and assigned_at is not null and gpu_count = 0
                  and rate_class in ('auto', 'pinned')
                  and id > (select uuidv7(- make_interval(secs => @arrival_seconds::float8)))
            ) gaps
        ) runs
    ) parts
    group by run, part
) batches
having count(*) > 0
order by 1, 2, 3;

-- name: BatchWaits :many
-- How long, in seconds, each owner's arrival batch stays open: until quiet
-- passes after its newest container, and at most max_seconds after its
-- first. An arrival is a container of the lookback, max and quiet seconds
-- that may need a purchase: still pending, or placed on a host bought for
-- it. It belongs to its workspace's connection, a mirror build to the
-- platform (a null connection), as pending demand does; work pinned to a
-- joined machine buys nothing. A batch runs back from its newest arrival
-- through arrivals less than quiet apart; one that may have begun before
-- the lookback, or a sample that fills, holds nothing. The sample follows
-- the primary key, so it reads at most sample_size rows whatever the
-- history or backlog.
with sample as (
    select c.created_at, c.workspace_id, c.release_id, c.image_build_id, c.state, c.capacity_host_id, c.billing_owner
    from containers c
    where c.id > (select uuidv7(- make_interval(secs => @lookback_seconds::float8)))
    order by c.id desc
    limit @sample_size
), recent as (
    select s.created_at, ws.connection_id
    from sample s
    left join image_builds ib on ib.id = s.image_build_id
    left join workspaces ws on ws.id = s.workspace_id and ib.mirror is not true
    left join releases r on r.id = s.release_id
    where (s.state = 'pending' or s.capacity_host_id is not null) and s.billing_owner <> 'self_hosted'
      and coalesce(r.spec -> 'placement' ->> 'machine', '') = ''
), arrivals as (
    select connection_id, created_at,
           coalesce(created_at - lag(created_at) over (partition by connection_id order by created_at),
                    created_at - (now() - make_interval(secs => @lookback_seconds::float8)))
               >= make_interval(secs => @quiet_seconds::float8) as opens
    from recent
), batch as (
    select connection_id, max(created_at) as newest, max(created_at) filter (where opens) as began
    from arrivals
    group by connection_id
)
select connection_id,
       greatest(extract(epoch from least(newest + make_interval(secs => @quiet_seconds::float8),
                                         began + make_interval(secs => @max_seconds::float8)) - now()), 0)::float8
           as wait_seconds
from batch
where began is not null and (select count(*) from sample) < @sample_size::int
order by connection_id;

-- name: PlannerCooldowns :many
-- Offers cooling now.
select connection_key, region, availability_zone_id, instance_type, market, until
from capacity_cooldowns
where until > now()
order by connection_key, region, availability_zone_id, instance_type, market;

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
                   hourly_micros, reserve_mode, replaces, holds_cpu_millis, holds_memory_bytes)
select v.id, 'lazycloud-' || v.instance_type, 'offline', v.kind, 'aws', v.connection_id, 'requested',
       'Waiting for the machine to be launched', v.cpu_millis, v.memory_bytes, v.gpu_type, v.gpu_count, v.region,
       v.availability_zone, v.availability_zone_id, v.instance_type, v.market, v.hourly_micros, v.reserve_mode,
       v.replaces, v.holds_cpu_millis, v.holds_memory_bytes
from jsonb_to_recordset(@hosts::jsonb) as v(
    id uuid, kind text, connection_id uuid, cpu_millis bigint, memory_bytes bigint, gpu_type text, gpu_count integer,
    region text, availability_zone text, availability_zone_id text, instance_type text, market text,
    hourly_micros bigint, reserve_mode text, replaces uuid, holds_cpu_millis bigint,
    holds_memory_bytes bigint);

-- name: ResumeReserves :many
-- Starts stopped reserves: to serve, the reserve mode cleared, or to
-- refresh, kept so the host prepares and stops again once it joins. Only a
-- requested resume serves.
update hosts h
set phase = 'resuming', phase_message = 'Starting from the reserve', phase_at = now(),
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
-- cool in every zone of their region.
insert into capacity_cooldowns (connection_key, region, instance_type, market, until, reason)
select v.connection_key, v.region, v.instance_type, v.market, now() + make_interval(secs => @seconds::float8),
       'the host bought for a container could not take it'
from jsonb_to_recordset(@offers::jsonb) as v(connection_key text, region text, instance_type text, market text)
on conflict (connection_key, region, availability_zone_id, instance_type, market)
do update set until = excluded.until, reason = excluded.reason;

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

-- name: HostWaitTrace :one
-- The trace of the longest-waiting pending container the fleet bought or
-- resumed the host for, read through the pending containers.
select coalesce((
    select c.traceparent from containers c
    where c.state = 'pending' and c.capacity_host_id = @host_id and c.traceparent is not null
    order by c.created_at limit 1
), '')::text as traceparent;
