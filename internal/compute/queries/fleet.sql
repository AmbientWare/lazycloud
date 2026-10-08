-- name: ClaimLaunches :many
-- Requested hosts whose launch is not held by another launcher. The lease
-- outlasts one pool's RunInstances call and each move to a next pool renews
-- it; a launcher that dies leaves it to expire.
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
returning h.id, h.kind, h.connection_id, h.region, h.availability_zone, h.instance_type, h.market, h.gpu_type,
          h.gpu_count, h.cpu_millis, h.memory_bytes, h.launch_attempts, h.launch_pools, h.reserve_mode, h.replaces,
          h.holds_cpu_millis, h.holds_memory_bytes;

-- name: MoveLaunchPool :execrows
-- Moves a requested host whose pool EC2 refused to the next pool at that
-- pool's cost and capacity, and renews the launcher's lease. Every pool it
-- moves to holds what it was bought to hold. A host another launcher moved
-- on since it was claimed stays where that one put it.
update hosts
set instance_type = @instance_type, region = @region, availability_zone = @availability_zone,
    availability_zone_id = @availability_zone_id, hourly_micros = @hourly_micros,
    cpu_millis = @cpu_millis, memory_bytes = @memory_bytes, launch_pools = launch_pools + 1,
    launch_lease_until = now() + make_interval(secs => @lease_seconds::float8), updated_at = now()
where id = @id and phase = 'requested' and launch_pools = @launch_pools;

-- name: HostWaiters :many
-- The placement each pending container waiting for a host asks for. A
-- build has no release and takes any region on Spot.
select coalesce(r.spec -> 'placement' ->> 'region', '')::text as region,
       coalesce(r.spec -> 'placement' ->> 'availability_zone', '')::text as zone,
       coalesce((r.spec -> 'placement' ->> 'preemptible')::boolean, true)::bool as preemptible
from containers c
left join releases r on r.id = c.release_id
where c.state = 'pending' and c.capacity_host_id = @host_id;

-- name: RecordLaunch :execrows
-- Records the instance launched in the pool the launcher holds; a host
-- another launcher moved on records nothing, and its instance ends.
update hosts
set instance_id = @instance_id, availability_zone = @availability_zone, availability_zone_id = @availability_zone_id,
    phase = 'provisioning', phase_message = 'Instance is starting; waiting for the node to report', phase_at = now(),
    launched_at = now(), launch_lease_until = null, updated_at = now(),
    authorization_id = sqlc.narg(authorization_id), node_role_arn = @node_role_arn,
    spot_request_id = sqlc.narg(spot_request_id), node_image = @node_image,
    hibernation_configured = @hibernation_configured
where id = @id and phase = 'requested' and launch_pools = @launch_pools;

-- name: FailHost :execrows
-- Fails a host still in the phase its caller read.
update hosts
set phase = 'failed', failure = @failure, phase_message = @message, phase_at = now(), state = 'retired',
    token_hash = null, launch_lease_until = null, updated_at = now()
where id = @id and phase = @from_phase;

-- name: RefuseRightsize :exec
-- Records that EC2 refused the launch bought to replace this host.
update hosts set rightsize_refused_at = now(), updated_at = now() where id = @id;

-- name: InsertCooldown :exec
-- Cools an offer in one zone, or in its whole region for the zone ''.
insert into capacity_cooldowns (connection_key, region, availability_zone_id, instance_type, market, until, reason)
values (@connection_key, @region, @availability_zone_id, @instance_type, @market, now() + make_interval(secs => @seconds::float8),
        @reason)
on conflict (connection_key, region, availability_zone_id, instance_type, market)
do update set until = excluded.until, reason = excluded.reason;

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
