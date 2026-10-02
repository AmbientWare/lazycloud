-- name: TryCapacityLock :one
-- One capacity controller decides at a time, so two passes never buy for
-- the same shortfall.
select pg_try_advisory_xact_lock(hashtextextended('capacity', 0))::bool;

-- name: InFlightHosts :many
-- Cloud hosts bought and not ready yet: their capacity is on the way.
select id, kind, connection_id, region, availability_zone, availability_zone_id, market, gpu_type, gpu_count,
       cpu_millis, memory_bytes
from hosts
where provider = 'aws' and phase in ('requested', 'provisioning', 'booting', 'joining')
order by id;

-- name: PendingDemand :many
-- Pending containers with what they need from a host, oldest first.
select c.id, c.workspace_id, c.cpu_millis, c.memory_bytes, c.capacity_wait, ws.connection_id,
       c.capacity_host_id, coalesce(bh.phase, '')::text as bought_phase, coalesce(bh.region, '')::text as bought_region,
       coalesce(bh.instance_type, '')::text as bought_type, coalesce(bh.market, '')::text as bought_market,
       coalesce(r.spec -> 'placement' ->> 'machine', '')::text as machine,
       coalesce(r.spec -> 'placement' ->> 'region', '')::text as region,
       coalesce(r.spec -> 'placement' ->> 'availability_zone', '')::text as zone,
       coalesce((r.spec -> 'placement' ->> 'preemptible')::boolean, true)::bool as preemptible,
       array(select jsonb_array_elements_text(coalesce(r.spec -> 'resources' -> 'gpu', build_gpus(c.image_build_id), '[]'::jsonb)))::text[] as gpus,
       coalesce((r.spec -> 'resources' ->> 'gpu_count')::int, 0)::int as gpu_count
from containers c
join workspaces ws on ws.id = c.workspace_id
left join releases r on r.id = c.release_id
left join hosts bh on bh.id = c.capacity_host_id
where c.state = 'pending'
order by c.created_at, c.id
limit @batch_size;

-- name: LiveCloudHostCounts :many
-- Live cloud hosts per owner: a connection id, or platform.
select coalesce(connection_id::text, 'platform')::text as owner, count(*)::int as hosts
from hosts
where provider = 'aws' and phase not in ('deleted', 'failed')
group by 1;

-- name: ActiveCooldowns :many
select connection_key, region, instance_type, market from capacity_cooldowns where until > now();

-- name: HostingConnections :many
-- Connections that take new workloads, with the networks of their active
-- authorization.
select cc.id, a.networks
from cloud_connections cc
join cloud_authorizations a on a.connection_id = cc.id and a.slot = 'active' and a.phase = 'ready'
where cc.phase in ('ready', 'reconnect_pending', 'retiring_authorization');

-- name: InsertRequestedHost :one
insert into hosts (name, state, kind, provider, connection_id, phase, phase_message, cpu_millis, memory_bytes,
                   gpu_type, gpu_count, region, availability_zone, availability_zone_id, instance_type, market,
                   hourly_micros)
values (@name, 'offline', @kind, 'aws', sqlc.narg(connection_id), 'requested', 'Waiting for the machine to be launched',
        @cpu_millis, @memory_bytes, @gpu_type, @gpu_count, @region, @availability_zone, @availability_zone_id,
        @instance_type, @market, @hourly_micros)
returning id;

-- name: SetCapacityWaits :execrows
-- Records why each pending container waits; unchanged rows are not
-- written.
update containers c
set capacity_wait = nullif(w.wait, ''), capacity_host_id = nullif(w.host, '00000000-0000-0000-0000-000000000000'::uuid)
from (select unnest(@ids::uuid[]) as id, unnest(@waits::text[]) as wait, unnest(@hosts::uuid[]) as host) w
where c.id = w.id and c.state = 'pending'
  and (c.capacity_wait is distinct from nullif(w.wait, '')
       or c.capacity_host_id is distinct from nullif(w.host, '00000000-0000-0000-0000-000000000000'::uuid));

-- name: ClaimLaunches :many
-- Requested hosts whose launch is not held by another launcher. The lease
-- outlasts one RunInstances call; a launcher that dies leaves it to expire.
update hosts h
set launch_lease_until = now() + make_interval(secs => @lease_seconds::float8),
    launch_attempts = launch_attempts + 1, updated_at = now()
where h.id in (
    select r.id from hosts r
    where r.provider = 'aws' and r.phase = 'requested'
      and (r.launch_lease_until is null or r.launch_lease_until < now())
    order by r.created_at
    limit @batch_size
    for update skip locked
)
returning h.id, h.kind, h.connection_id, h.region, h.availability_zone, h.instance_type, h.market, h.gpu_count,
          h.launch_attempts, h.reserve_mode;

-- name: RecordLaunch :execrows
update hosts
set instance_id = @instance_id, availability_zone = @availability_zone, availability_zone_id = @availability_zone_id,
    phase = 'provisioning', phase_message = 'Instance is starting; waiting for the node to report', phase_at = now(),
    launched_at = now(), launch_lease_until = null, updated_at = now(),
    authorization_id = sqlc.narg(authorization_id), node_role_arn = @node_role_arn,
    spot_request_id = sqlc.narg(spot_request_id), node_image = @node_image,
    hibernation_configured = @hibernation_configured
where id = @id and phase = 'requested';

-- name: FailHost :execrows
-- Fails a host still in the phase its caller read.
update hosts
set phase = 'failed', failure = @failure, phase_message = @message, phase_at = now(), state = 'retired',
    token_hash = null, launch_lease_until = null, updated_at = now()
where id = @id and phase = @from_phase;

-- name: InsertCooldown :exec
insert into capacity_cooldowns (connection_key, region, instance_type, market, until, reason, refused_at)
values (@connection_key, @region, @instance_type, @market, now() + make_interval(secs => @seconds::float8), @reason, now())
on conflict (connection_key, region, instance_type, market)
do update set until = excluded.until, reason = excluded.reason, refused_at = excluded.refused_at;

-- name: MarkIdle :exec
-- A ready cloud host is idle from when its last container stopped. The WHERE
-- picks only hosts whose idle_since disagrees with their live containers, so
-- each update flips it: set while unset, cleared while set.
update hosts h
set idle_since = case when h.idle_since is null then now() end
where h.provider = 'aws' and h.phase = 'ready'
  and (h.idle_since is null) = not exists (select 1 from containers c where c.host_id = h.id and c.state <> 'stopped');

-- name: IdleHosts :many
-- Ready cloud hosts idle past the window, oldest first within each market.
select h.id, coalesce(h.connection_id::text, 'platform')::text as owner, h.region, coalesce(h.market, '')::text as market,
       h.idle_since
from hosts h
where h.provider = 'aws' and h.phase = 'ready' and h.capacity_state = 'available'
  and h.idle_since is not null
order by h.idle_since, h.id;

-- name: DrainHosts :many
-- Moves idle hosts to draining; a host that took a container since the
-- scan stays.
update hosts h
set phase = 'draining', capacity_state = 'draining', capacity_reason = @reason,
    phase_message = 'Draining; no new work is placed here', phase_at = now(), updated_at = now()
where h.id = any(@ids::uuid[]) and h.phase = 'ready'
  and not exists (select 1 from containers c where c.host_id = h.id and c.state <> 'stopped')
returning h.id;

-- name: DrainConnectionHosts :many
-- A disconnecting account's hosts stop taking work.
update hosts h
set phase = 'draining', capacity_state = 'draining', capacity_reason = 'disconnecting',
    phase_message = 'Draining; no new work is placed here', phase_at = now(), updated_at = now()
from cloud_connections cc
where cc.id = h.connection_id and cc.phase = 'disconnect_draining' and h.phase in ('ready', 'joining')
returning h.id;

-- name: CancelConnectionLaunches :exec
-- A disconnecting account buys nothing more.
update hosts h
set phase = 'deleted', phase_message = 'Removed', phase_at = now(), state = 'retired', updated_at = now()
from cloud_connections cc
where cc.id = h.connection_id and cc.phase = 'disconnect_draining' and h.phase = 'requested'
  and (h.launch_lease_until is null or h.launch_lease_until < now());

-- name: ClaimTerminations :many
-- Draining cloud hosts that run nothing move to terminating.
update hosts h
set phase = 'terminating', phase_message = 'Shutting down', phase_at = now(), state = 'retired',
    token_hash = null, updated_at = now()
where h.provider = 'aws' and h.phase = 'draining' and h.instance_id is not null
  and not exists (select 1 from containers c where c.host_id = h.id and c.state <> 'stopped')
returning h.id, h.connection_id, h.region, h.instance_id;

-- name: ManagedConnectionNetworks :many
-- Connections whose account may still hold fleet instances, with the
-- networks of their active authorization.
select cc.id, a.networks
from cloud_connections cc
join cloud_authorizations a on a.connection_id = cc.id and a.slot = 'active' and a.phase = 'ready'
where cc.phase not in ('awaiting_authorization', 'validating');

-- name: FleetHostsInRegion :many
-- Cloud hosts of one owner and region the provider should know about.
select id, phase, state, instance_id, launched_at, last_seen_at, phase_at, updating_until, stop_requested_at
from hosts
where provider = 'aws' and region = @region
  and connection_id is not distinct from sqlc.narg(connection_id)::uuid
  and phase not in ('deleted', 'failed', 'requested')
order by id;

-- name: FleetRegions :many
-- Owner and region pairs with cloud hosts to reconcile.
select distinct connection_id, region from hosts
where provider = 'aws' and phase not in ('deleted', 'failed', 'requested') and region <> '';

-- name: KnownHostIDs :many
-- Which of these hosts still exist and are not failed or deleted.
select id from hosts where id = any(@ids::uuid[]) and phase not in ('deleted', 'failed');

-- name: SetHostPhase :execrows
update hosts
set phase = @phase, phase_message = @message, phase_at = now(), updated_at = now()
where id = @id and phase = @from_phase;

-- name: MarkHostDeleted :execrows
-- Deletes a host still in the phase its caller read.
update hosts
set phase = 'deleted', phase_message = 'Removed', phase_at = now(), state = 'retired', token_hash = null, updated_at = now()
where id = @id and phase = @from_phase;

-- name: ConnectionScope :one
select a.id as authorization_id, a.role_arn, a.external_id, a.networks, a.node_instance_profile,
       a.node_role_arn, cc.phase
from cloud_connections cc
join cloud_authorizations a on a.connection_id = cc.id and a.slot = 'active'
where cc.id = @id;
