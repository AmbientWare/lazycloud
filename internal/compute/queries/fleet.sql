-- name: MoveLaunchPool :execrows
-- Moves a requested host whose pool EC2 refused to the next pool at that
-- pool's cost and capacity, with its attempts counted afresh there, and
-- renews the claim's lease for lease_seconds, which 0 releases. Every pool
-- it moves to holds what it was bought to hold.
update hosts
set instance_type = @instance_type, region = @region, availability_zone = @availability_zone,
    availability_zone_id = @availability_zone_id, hourly_micros = @hourly_micros,
    cpu_millis = @cpu_millis, memory_bytes = @memory_bytes, launch_pools = launch_pools + 1, launch_attempts = 0,
    launch_lease_until = now() + make_interval(secs => @lease_seconds::float8),
    updated_at = now()
where id = @id and action_token = @token and phase = 'requested';

-- name: HostWaiters :many
-- The placement each pending container waiting for a host asks for, the
-- disks it attaches, and the bucket region and measured bytes of the
-- platform volumes its start reads. A build has no release and takes any
-- region on Spot.
select coalesce(r.spec -> 'placement' ->> 'region', '')::text as region,
       coalesce(r.spec -> 'placement' ->> 'availability_zone', '')::text as zone,
       coalesce((r.spec -> 'placement' ->> 'preemptible')::boolean, true)::bool as preemptible,
       coalesce(jsonb_array_length(r.spec -> 'disks'), 0)::int as disks,
       sb.region as read_region, coalesce(vb.bytes, 0)::bigint as read_bytes
from containers c
left join releases r on r.id = c.release_id
cross join lateral (select volume_read_bytes(c.workspace_id, r.spec -> 'volumes') as bytes) vb
left join workspace_buckets sb on sb.workspace_id = c.workspace_id and vb.bytes is not null
where c.state = 'pending' and c.capacity_host_id = @host_id;

-- name: RecordLaunch :execrows
-- Records the instance launched in the pool the claim holds; a host the
-- claim no longer holds records nothing, and its instance ends.
update hosts
set instance_id = @instance_id, availability_zone = @availability_zone, availability_zone_id = @availability_zone_id,
    phase = 'provisioning', phase_message = 'Instance is starting; waiting for the node to report', phase_at = now(),
    launched_at = now(), launch_lease_until = null, updated_at = now(),
    authorization_id = sqlc.narg(authorization_id), node_role_arn = @node_role_arn,
    spot_request_id = sqlc.narg(spot_request_id), node_image = @node_image,
    hibernation_configured = @hibernation_configured
where id = @id and action_token = @token and phase = 'requested';

-- name: FailLaunch :execrows
-- Fails a requested host the claim holds that could not launch.
update hosts
set phase = 'failed', failure = @failure, phase_message = @message, phase_at = now(), state = 'retired',
    token_hash = null, launch_lease_until = null, updated_at = now()
where id = @id and action_token = @token and phase = 'requested';

-- name: FailHost :execrows
-- Fails a host still in the phase its caller read whose instance is gone.
update hosts
set phase = 'failed', failure = @failure, phase_message = @message, phase_at = now(), state = 'retired',
    token_hash = null, launch_lease_until = null, updated_at = now()
where id = @id and phase = @from_phase;

-- name: EndHost :execrows
-- A host still in the phase its caller read whose instance must end
-- terminates with its failure recorded; the actuator terminates the
-- instance, and the host fails once EC2 reports it gone.
update hosts
set phase = 'terminating', failure = @failure, phase_message = @message, phase_at = now(), state = 'retired',
    token_hash = null, updated_at = now()
where id = @id and phase = @from_phase;

-- name: RefuseRightsize :exec
-- Records that EC2 refused the launch bought to replace this host.
update hosts set rightsize_refused_at = now(), updated_at = now() where id = @id;

-- name: InsertCooldown :exec
-- Cools an offer in one zone, or in its whole region for the zone '', for
-- seconds, and records the launch walk that refused it, if any. Every
-- cooldown of an offer is written here. Refused
-- again while it cools, it keeps its end and the walk that refused it
-- first, so no walk moves another walk's end; refused after its cooldown
-- ended, it cools twice as long as that cooldown lasted, up to max_seconds.
insert into capacity_cooldowns (connection_key, region, availability_zone_id, instance_type, market, until, cooled_for, reason, walk)
values (@connection_key, @region, @availability_zone_id, @instance_type, @market, now() + make_interval(secs => @seconds::float8),
        make_interval(secs => @seconds::float8), @reason, sqlc.narg('walk'))
on conflict (connection_key, region, availability_zone_id, instance_type, market)
do update set
    cooled_for = case when capacity_cooldowns.until > now() then capacity_cooldowns.cooled_for
                      else least(greatest(capacity_cooldowns.cooled_for * 2, excluded.cooled_for), make_interval(secs => @max_seconds::float8)) end,
    until = case when capacity_cooldowns.until > now() then capacity_cooldowns.until
                 else now() + least(greatest(capacity_cooldowns.cooled_for * 2, excluded.cooled_for), make_interval(secs => @max_seconds::float8)) end,
    reason = excluded.reason,
    walk = case when capacity_cooldowns.until > now() then coalesce(capacity_cooldowns.walk, excluded.walk) else excluded.walk end;

-- name: AlignWalk :exec
-- Every pool one launch walk refused, in this pass or an earlier one,
-- cools until the latest of their ends, so the planner buys for them again
-- once. The rows lock in key order; a deadlock with another walk fails
-- this move, and the walk goes on next pass.
with walked as (
    select connection_key, region, availability_zone_id, instance_type, market, until
    from capacity_cooldowns
    where capacity_cooldowns.walk = @walk::uuid
    order by connection_key, region, availability_zone_id, instance_type, market
    for update
)
update capacity_cooldowns cd set until = (select max(until) from walked)
from walked w
where (cd.connection_key, cd.region, cd.availability_zone_id, cd.instance_type, cd.market)
    = (w.connection_key, w.region, w.availability_zone_id, w.instance_type, w.market);

-- name: ClearCooldown :exec
-- A launch in a zone ends the refusals of its pool there and in its region.
delete from capacity_cooldowns
where connection_key = @connection_key and region = @region and availability_zone_id in (@availability_zone_id, '')
  and instance_type = @instance_type and market = @market;

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

-- name: ManagedConnectionNetworks :many
-- Connections whose capacity the platform still manages, with the
-- networks of their active authorization; Connection.ManagesCapacity
-- applies the same rule. A removal sweeps its own instances.
select cc.id, a.networks
from cloud_connections cc
join cloud_authorizations a on a.connection_id = cc.id and a.slot = 'active' and a.phase = 'ready'
where cc.phase not in ('revoking', 'verifying_revocation', 'action_required')
   or (cc.phase = 'action_required'
       and exists (select 1 from cloud_authorizations r where r.connection_id = cc.id and r.slot = 'retiring'));

-- name: FleetHostsInRegion :many
-- Cloud hosts of one owner and region the provider should know about.
select id, phase, phase_message, state, instance_id, launched_at, last_seen_at, phase_at, updating_until, stop_requested_at,
       interruption_at, failure
from hosts
where provider = 'aws' and region = @region
  and connection_id is not distinct from sqlc.narg(connection_id)::uuid
  and phase not in ('deleted', 'failed', 'requested')
order by id;

-- name: FleetRegions :many
-- Owner and region pairs with cloud hosts to reconcile.
select distinct connection_id, region from hosts
where provider = 'aws' and phase not in ('deleted', 'failed', 'requested') and region <> '';

-- name: LiveHosts :many
-- Which of these hosts are neither failed nor deleted, with the instance and
-- Spot request each records.
select id, instance_id, spot_request_id from hosts where id = any(@ids::uuid[]) and phase not in ('deleted', 'failed');

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
       a.node_role_arn, cc.phase, a.phase as authorization_phase
from cloud_connections cc
join cloud_authorizations a on a.connection_id = cc.id and a.slot = 'active'
where cc.id = @id;
