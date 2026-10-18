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
-- containers reserve, what those that could run on Spot reserve, what each
-- of those that could move to a Spot host needs, whether one of them could
-- not, whether its agent must move to the target release first, and
-- whether EC2 refused its start within cooldown_seconds. Packed CPU work
-- counts apart: the containers and the newest placement
-- that keep a GPU host up are its GPU work.
select h.id, h.kind, h.connection_id, h.phase, h.phase_at, h.state, h.capacity_state,
       h.last_seen_at, h.session_epoch, h.region, h.availability_zone, h.availability_zone_id, h.instance_type,
       h.market, h.gpu_type, h.gpu_count, h.cpu_millis, h.memory_bytes, h.hourly_micros,
       h.interruption_at, h.reserve_mode, h.hibernation_configured, h.spot_request_id, h.image_evidence,
       h.prepared_agent_version, h.rollout_bucket, h.updating_until, h.replaces,
       h.rightsize_refused_at, h.hands_back_at, h.stopped_at,
       coalesce(h.start_refused_at > now() - make_interval(secs => @cooldown_seconds::float8), false)::bool as start_refused,
       coalesce(used.cpu, 0)::bigint as used_cpu, coalesce(used.memory, 0)::bigint as used_memory,
       coalesce(used.gpus, 0)::int as used_gpus, coalesce(used.containers, 0)::int as containers,
       coalesce(used.disks, 0)::int as used_disks,
       coalesce(used.stays, false)::bool as stays,
       coalesce(used.packed_cpu, 0)::bigint as packed_cpu, coalesce(used.packed_memory, 0)::bigint as packed_memory,
       coalesce(used.packed_ids, '{}')::uuid[] as packed_ids,
       coalesce(used.move_cpu, '{}')::bigint[] as move_cpu, coalesce(used.move_memory, '{}')::bigint[] as move_memory,
       coalesce(used.move_gpu_count, '{}')::int[] as move_gpu_count, coalesce(used.move_gpus, '{}')::text[] as move_gpus,
       coalesce(used.move_region, '{}')::text[] as move_region,
       exists (select 1 from agent_updates u where u.host_id = h.id)::bool as update_due
from hosts h
left join lateral (
    select sum(c.cpu_millis) as cpu, sum(c.memory_bytes) as memory,
           sum(m.gpus) as gpus,
           sum(c.cpu_millis) filter (where p.packed) as packed_cpu, sum(c.memory_bytes) filter (where p.packed) as packed_memory,
           array_agg(c.id order by c.id) filter (where p.packed) as packed_ids,
           bool_or(not m.moves) as stays,
           array_agg(c.cpu_millis) filter (where m.moves) as move_cpu,
           array_agg(c.memory_bytes) filter (where m.moves) as move_memory,
           array_agg(m.gpus) filter (where m.moves) as move_gpu_count,
           -- The GPU models each accepts, comma-separated.
           array_agg(array_to_string(array(select jsonb_array_elements_text(coalesce(r.spec -> 'resources' -> 'gpu', '[]'::jsonb))), ','))
               filter (where m.moves) as move_gpus,
           array_agg(coalesce(r.spec -> 'placement' ->> 'region', '')) filter (where m.moves) as move_region,
           sum(jsonb_array_length(coalesce(r.spec -> 'disks', '[]'::jsonb))) as disks,
           count(*) filter (where not p.packed) as containers
    from containers c
    left join releases r on r.id = c.release_id
    cross join lateral (
        select coalesce(c.purpose = 'serve' and release_moves(r.spec), false) as moves,
               case when c.image_build_id is null then coalesce(release_gpus(r.spec), 0) else c.gpu_count end as gpus
    ) m
    cross join lateral (select packed(h.provider, h.gpu_count, c.gpu_count) as packed) p
    where c.host_id = h.id and c.state <> 'stopped'
) used on true
where h.provider = 'aws' and h.phase not in ('deleted', 'failed')
order by h.id;

-- name: PendingDemand :many
-- The oldest pending containers, up to the batch, grouped by what they need
-- from a host, each with when it was created and the host bought for it
-- (the zero id for none). The
-- batch follows the pending index alone: ordering ties by id would sort a
-- whole backlog created in one statement. A mirror build runs on the
-- platform, as placement puts it, whatever its workspace's connection.
with batch as (
    select c.id, c.workspace_id, c.release_id, c.image_build_id, c.cpu_millis, c.memory_bytes, c.capacity_host_id, c.traceparent,
           c.purpose, c.created_at
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
       jsonb_array_length(coalesce(r.spec -> 'disks', '[]'::jsonb))::int as disks,
       sb.region as read_region, coalesce(vb.bytes, 0)::bigint as read_bytes,
       coalesce(b.purpose = 'serve' and release_packs(r.spec), false)::bool as packs,
       array_agg(b.id order by b.id)::uuid[] as ids,
       array_agg(coalesce(b.capacity_host_id, '00000000-0000-0000-0000-000000000000'::uuid) order by b.id)::uuid[]
           as bought,
       array_agg(coalesce(b.traceparent, '') order by b.id)::text[] as traceparents,
       array_agg(b.created_at order by b.id)::timestamptz[] as created
from batch b
left join image_builds ib on ib.id = b.image_build_id
left join workspaces ws on ws.id = b.workspace_id and ib.mirror is not true
left join releases r on r.id = b.release_id
cross join lateral (select volume_read_bytes(b.workspace_id, r.spec -> 'volumes') as bytes) vb
left join workspace_buckets sb on sb.workspace_id = b.workspace_id and vb.bytes is not null
group by 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13
order by 4, 5, 6, 2 desc, 3 desc;

-- name: RecentShapes :many
-- By account (its owner key: platform or the connection id), purchase market and
-- GPU model: the largest CPU, memory and GPUs placed fleet containers
-- reserved, the most disks one attached, and the bytes of volumes they
-- read with how many started, among the newest
-- containers created within the window, up to the sample (recent); the
-- largest shape among those created within the warm window (started),
-- which sizes the warm floor; the largest batch of them within the warm
-- window (burst), which the market keeps room for; and what the placed
-- Spot-tolerant CPU containers of the sample created within the
-- arrival window reserve, less their largest batch (arrived): a steady rate
-- of work, which a single burst is not. A batch is
-- arrivals less than batch_quiet apart, at most batch_max long, as
-- BatchWaits forms them. The read follows the primary key, so it reads at
-- most sample_size containers whatever the history; the subqueries make
-- its bounds constants the index can use.
with recent as (
    select c.id, c.created_at, c.cpu_millis, c.memory_bytes, c.gpu_count, c.gpu_type, c.rate_class,
           coalesce(case when c.billing_owner = 'connected_cloud' then ws.connection_id::text end, 'platform') as owner,
           coalesce(jsonb_array_length(r.spec -> 'disks'), 0) as disks,
           coalesce(volume_read_bytes(c.workspace_id, r.spec -> 'volumes'), 0) as read_bytes
    from (
        select c.id, c.created_at, c.cpu_millis, c.memory_bytes, c.gpu_count, c.gpu_type, c.rate_class, c.billing_owner, c.assigned_at,
               c.release_id, c.workspace_id
        from containers c
        where c.id > (select uuidv7(- make_interval(secs => @window_seconds::float8)))
        order by c.id desc
        limit @sample_size
    ) c
    left join releases r on r.id = c.release_id
    left join workspaces ws on ws.id = c.workspace_id
    where c.billing_owner <> 'self_hosted' and c.assigned_at is not null
)
select owner::text as owner, 'recent'::text as kind, (rate_class in ('auto', 'pinned'))::bool as preemptible, gpu_type,
       max(cpu_millis)::bigint as cpu_millis, max(memory_bytes)::bigint as memory_bytes, max(gpu_count)::int as gpus,
       max(disks)::int as disks, sum(read_bytes)::bigint as read_bytes, count(*)::int as starts
from recent
group by 1, 3, 4
union all
select owner, 'started', (rate_class in ('auto', 'pinned'))::bool, gpu_type,
       max(cpu_millis)::bigint, max(memory_bytes)::bigint, max(gpu_count)::int, 0, 0, count(*)::int
from recent
where id > (select uuidv7(- make_interval(secs => @warm_seconds::float8)))
group by 1, 3, 4
union all
select owner, 'burst', preemptible, gpu_type, max(cpu)::bigint, max(memory)::bigint, max(gpus)::int, 0, 0, 0
from (
    select owner, preemptible, gpu_type, sum(cpu_millis) as cpu, sum(memory_bytes) as memory, sum(gpu_count) as gpus
    from (
        select owner, preemptible, gpu_type, cpu_millis, memory_bytes, gpu_count, run,
               floor(extract(epoch from created_at - min(created_at) over (partition by owner, preemptible, gpu_type, run))
                     / @batch_max_seconds::float8) as part
        from (
            select owner, preemptible, gpu_type, cpu_millis, memory_bytes, gpu_count, created_at,
                   count(*) filter (where opens) over (partition by owner, preemptible, gpu_type order by created_at, id) as run
            from (
                select id, owner, (rate_class in ('auto', 'pinned'))::bool as preemptible, gpu_type, cpu_millis, memory_bytes, gpu_count,
                       created_at,
                       coalesce(created_at - lag(created_at)
                                over (partition by owner, (rate_class in ('auto', 'pinned')), gpu_type order by created_at, id),
                                interval '1 day') >= make_interval(secs => @batch_quiet_seconds::float8) as opens
                from recent
                where id > (select uuidv7(- make_interval(secs => @warm_seconds::float8)))
            ) gaps
        ) runs
    ) parts
    group by owner, preemptible, gpu_type, run, part
) batches
group by owner, preemptible, gpu_type
union all
select owner, 'arrived', true, '', (sum(cpu) - max(cpu))::bigint, (sum(memory) - max(memory))::bigint, 0, 0, 0, 0
from (
    select owner, sum(cpu_millis) as cpu, sum(memory_bytes) as memory
    from (
        select owner, cpu_millis, memory_bytes, run,
               floor(extract(epoch from created_at - min(created_at) over (partition by owner, run)) / @batch_max_seconds::float8) as part
        from (
            select owner, cpu_millis, memory_bytes, created_at,
                   count(*) filter (where opens) over (partition by owner order by created_at, id) as run
            from (
                select id, owner, cpu_millis, memory_bytes, created_at,
                       coalesce(created_at - lag(created_at) over (partition by owner order by created_at, id), interval '1 day')
                           >= make_interval(secs => @batch_quiet_seconds::float8) as opens
                from recent
                where gpu_count = 0
                  and rate_class in ('auto', 'pinned')
                  and id > (select uuidv7(- make_interval(secs => @arrival_seconds::float8)))
            ) gaps
        ) runs
    ) parts
    group by owner, run, part
) batches
group by owner
order by 1, 2, 3, 4;

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

-- name: InsertRequestedHosts :exec
-- Hosts to buy, for the launcher. The planner names each id, so container
-- waits can point at a host bought in the same statement. A host's name
-- ends with its id's random last group, unique where the id's leading,
-- time-ordered digits are not.
insert into hosts (id, name, state, kind, provider, connection_id, phase, phase_message, cpu_millis, memory_bytes,
                   gpu_type, gpu_count, region, availability_zone, availability_zone_id, instance_type, market,
                   hourly_micros, reserve_mode, replaces, holds_cpu_millis, holds_memory_bytes, holds_disks, margin_waived)
select v.id, 'lazycloud-' || split_part(v.id::text, '-', 5), 'offline', v.kind, 'aws', v.connection_id, 'requested',
       'Waiting for the machine to be launched', v.cpu_millis, v.memory_bytes, v.gpu_type, v.gpu_count, v.region,
       v.availability_zone, v.availability_zone_id, v.instance_type, v.market, v.hourly_micros, v.reserve_mode,
       v.replaces, v.holds_cpu_millis, v.holds_memory_bytes, v.holds_disks, v.margin_waived
from jsonb_to_recordset(@hosts::jsonb) as v(
    id uuid, kind text, connection_id uuid, cpu_millis bigint, memory_bytes bigint, gpu_type text, gpu_count integer,
    region text, availability_zone text, availability_zone_id text, instance_type text, market text,
    hourly_micros bigint, reserve_mode text, replaces uuid, holds_cpu_millis bigint,
    holds_memory_bytes bigint, holds_disks smallint, margin_waived boolean);

-- name: ResumeReserves :many
-- Starts stopped reserves to serve, their reserve mode cleared; a stopping
-- one whose instance stopped starts once EC2 disables its Spot request, and
-- keeps when it stopped, which bounds that wait. Only a requested resume
-- serves.
update hosts
set phase = 'resuming', phase_message = 'Starting from the reserve', phase_at = now(),
    reserve_mode = null, stopped_at = case when phase = 'stopping' then stopped_at end, updated_at = now()
where id = any(@ids::uuid[]) and (phase = 'stopped' or phase = 'stopping' and stopped_at is not null)
returning id;

-- name: ReturnToReserve :many
-- Idle serving hosts start proving they may stop into the reserve, each in
-- the mode the planner chose; a host that took a container since the
-- snapshot stays. Packed CPU work, which the planner evicts, holds none.
update hosts h
set phase = 'preparing', phase_message = 'Preparing to stop into the reserve', phase_at = now(),
    reserve_mode = case when h.id = any(@hibernate_ids::uuid[]) then 'hibernate' else 'stop' end,
    hands_back_at = null, updated_at = now()
where h.id = any(@ids::uuid[]) and h.phase = 'ready' and h.capacity_state = 'available'
  and not exists (
      select 1 from containers c where c.host_id = h.id and c.state <> 'stopped' and not packed(h.provider, h.gpu_count, c.gpu_count)
  )
returning h.id;

-- name: DrainHosts :many
-- Idle serving hosts, and reserves still preparing, drain toward
-- termination; a host that took a container since the snapshot stays.
-- Packed CPU work, which the planner evicts, holds none.
update hosts h
set phase = 'draining', capacity_state = 'draining', capacity_reason = @reason, reserve_mode = null,
    hands_back_at = null, phase_message = 'Draining; no new work is placed here', phase_at = now(), updated_at = now()
where h.id = any(@ids::uuid[]) and h.phase in ('ready', 'preparing')
  and not exists (
      select 1 from containers c where c.host_id = h.id and c.state <> 'stopped' and not packed(h.provider, h.gpu_count, c.gpu_count)
  )
returning h.id;

-- name: SetHandsBack :many
-- Serving on-demand hosts start handing Spot-tolerant work back, those of
-- @back_ids, or lend to it again; one that started keeps its time.
update hosts
set hands_back_at = case when id = any(@back_ids::uuid[]) then coalesce(hands_back_at, now()) end, updated_at = now()
where id = any(@back_ids::uuid[] || @lend_ids::uuid[]) and phase = 'ready' and market = 'on_demand'
returning id;

-- name: RetireReserves :many
-- Stopped reserves terminate; a host not launched yet, a reserve or one
-- nothing waits for, is removed unless a launcher holds it or a pending
-- container still waits for it.
update hosts h
set phase = case when h.phase = 'requested' then 'deleted' else 'terminating' end,
    phase_message = case when h.phase = 'requested' then 'Removed' else 'Shutting down' end,
    phase_at = now(), state = 'retired', token_hash = null, updated_at = now()
where h.id = any(@ids::uuid[])
  and (h.phase = 'stopped'
       or (h.phase = 'requested' and (h.launch_lease_until is null or h.launch_lease_until < now())
           and not exists (select 1 from containers c where c.capacity_host_id = h.id and c.state = 'pending')))
returning h.id;

-- name: SetCapacityWaits :execrows
-- Records why each pending container waits; unchanged rows are not
-- written.
update containers c
set capacity_wait = nullif(w.wait, ''), capacity_host_id = nullif(w.host, '00000000-0000-0000-0000-000000000000'::uuid)
from (select unnest(@ids::uuid[]) as id, unnest(@waits::text[]) as wait, unnest(@hosts::uuid[]) as host) w
where c.id = w.id and c.state = 'pending'
  and (c.capacity_wait is distinct from nullif(w.wait, '')
       or c.capacity_host_id is distinct from nullif(w.host, '00000000-0000-0000-0000-000000000000'::uuid));

-- name: FailPreparing :many
-- Reserves whose agent never proved them within the limit terminate with
-- the failure recorded. An agent update in flight waits.
update hosts
set phase = 'terminating', failure = 'service_lost', phase_message = @message, phase_at = now(), state = 'retired',
    token_hash = null, updated_at = now()
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
