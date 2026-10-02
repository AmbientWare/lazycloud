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
