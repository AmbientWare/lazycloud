-- name: ClaimHostActions :many
-- Cloud hosts in one of @phases that wait on an EC2 call no other actuator
-- holds, oldest phase change first: a requested host to launch; a platform
-- host stopping or resuming; a host terminating whose last terminate EC2
-- has had @settle_seconds to list; a draining host that runs nothing, which
-- moves to terminating and wakes its session and the planners, though an
-- interrupted one is left for the provider to reclaim until
-- @reclaim_grace_seconds past its reclaim time. Each claim leases its row
-- for @lease_seconds under a new token.
with claimed as (
    select r.id, r.phase as claimed_phase from hosts r
    where r.provider = 'aws' and r.phase = any(@phases::text[])
      and (r.launch_lease_until is null or r.launch_lease_until < now())
      and case r.phase
          when 'requested' then true
          when 'terminating' then r.instance_id is not null and (r.terminate_requested_at is null
              or r.terminate_requested_at < now() - make_interval(secs => @settle_seconds::float8))
          when 'draining' then r.instance_id is not null
              and (r.interruption_at is null or r.interruption_at < now() - make_interval(secs => @reclaim_grace_seconds::float8))
              and not exists (select 1 from containers c where c.host_id = r.id and c.state <> 'stopped')
          else r.instance_id is not null and r.kind = 'platform'
          end
    order by r.phase_at, r.id
    limit @batch_size
    for update skip locked
), woken as (
    select count(pg_notify(c.channel, cl.id::text)) as n
    from claimed cl cross join (values ('lc_host'), ('lc_compute')) as c(channel)
    where cl.claimed_phase = 'draining'
)
update hosts h
set launch_lease_until = now() + make_interval(secs => @lease_seconds::float8), action_token = gen_random_uuid(),
    launch_attempts = h.launch_attempts + (h.phase = 'requested')::int,
    phase = case when h.phase = 'draining' then 'terminating' else h.phase end,
    phase_message = case when h.phase = 'draining' then 'Shutting down' else h.phase_message end,
    phase_at = case when h.phase = 'draining' then now() else h.phase_at end,
    state = case when h.phase = 'draining' then 'retired' else h.state end,
    token_hash = case when h.phase = 'draining' then null else h.token_hash end,
    updated_at = now()
from claimed cross join woken
where h.id = claimed.id
returning h.id, h.action_token, h.name, h.kind, h.connection_id, h.phase, h.region, h.availability_zone, h.availability_zone_id,
          h.instance_type, h.market, h.gpu_type, h.gpu_count, h.cpu_millis, h.memory_bytes, h.launch_attempts, h.launch_pools,
          h.reserve_mode, h.replaces, h.holds_cpu_millis, h.holds_memory_bytes, h.holds_disks, h.instance_id, h.spot_request_id,
          h.hibernation_configured, h.stop_requested_at, h.force_stop_at, h.hibernate_refused_at, h.stopped_at, h.margin_waived;

-- name: ReleaseHostActions :exec
-- Hosts a pass is done with come up again after @recheck_seconds, unless
-- another claim holds them now.
update hosts h
set launch_lease_until = now() + make_interval(secs => @recheck_seconds::float8), updated_at = now()
from (select unnest(@ids::uuid[]) as id, unnest(@tokens::uuid[]) as token) v
where h.id = v.id and h.action_token = v.token;

-- name: RecordTerminateRequested :execrows
-- EC2 accepted the terminate of a terminating host, or lists its instance
-- ending or gone; reconcile marks the host gone once EC2 confirms it.
update hosts set terminate_requested_at = now(), updated_at = now()
where id = @id and action_token = @token and phase = 'terminating';

-- name: RecordStopRequested :execrows
-- EC2 accepted a stop of a stopping host: a hibernation leaves its image
-- unknown until proven, a plain or forced stop saves none.
update hosts
set stop_requested_at = coalesce(stop_requested_at, now()),
    force_stop_at = case when @forced::bool then now() else force_stop_at end,
    image_evidence = @image_evidence, updated_at = now()
where id = @id and action_token = @token and phase = 'stopping';

-- name: RecordHibernateRefused :execrows
-- EC2 refused to hibernate a stopping host; the first refusal starts the
-- window after which it stops plainly.
update hosts
set hibernate_refused_at = coalesce(hibernate_refused_at, now()), updated_at = now()
where id = @id and action_token = @token and phase = 'stopping';

-- name: RecordInstanceStopped :exec
-- EC2 reports a stopping Spot host's instance stopped but holds its
-- persistent request active, so the host cannot start yet. A hibernation
-- still unproven takes the evidence EC2's stop reason gives.
update hosts
set stopped_at = now(),
    image_evidence = case when image_evidence = 'unknown' then @hibernation_evidence::text else image_evidence end,
    updated_at = now()
where id = @id and action_token = @token and phase = 'stopping' and stopped_at is null;

-- name: MarkReserveStopped :execrows
-- EC2 reports a stopping host's instance stopped and able to start. A
-- hibernation still unproven takes the evidence EC2's stop reason gives.
update hosts
set phase = 'stopped', phase_message = 'Stopped in the reserve', phase_at = now(), stopped_at = coalesce(stopped_at, now()),
    image_evidence = case when image_evidence = 'unknown' then @hibernation_evidence::text else image_evidence end,
    launch_lease_until = null, updated_at = now()
where id = @id and action_token = @token and phase = 'stopping';

-- name: RecordStartRequested :execrows
-- EC2 accepted the start of a resuming host, or reports it starting: the
-- last stop's facts end; its image evidence stays for the resume report.
update hosts
set stop_requested_at = null, force_stop_at = null, hibernate_refused_at = null, stopped_at = null,
    updated_at = now()
where id = @id and action_token = @token and phase = 'resuming';

-- name: ReturnStartToReserve :execrows
-- EC2 refused to start a resuming host for capacity: it stops in the
-- reserve again, hibernated where its image may be saved, and records the
-- refusal.
update hosts
set phase = 'stopped', phase_message = @message, phase_at = now(), start_refused_at = now(),
    reserve_mode = case when image_evidence in ('saved', 'unknown') then 'hibernate' else 'stop' end,
    launch_lease_until = null, updated_at = now()
where id = @id and action_token = @token and phase = 'resuming';

-- name: RetireReserve :execrows
-- A stopping host that cannot stop, as when EC2 refused the stop, retires,
-- and a purchase replaces it.
update hosts
set phase = 'terminating', phase_message = @message, phase_at = now(), state = 'retired', token_hash = null,
    launch_lease_until = null, updated_at = now()
where id = @id and action_token = @token and phase = 'stopping';

-- name: SpotRequestOfInstance :one
-- The persistent Spot request that launched an instance, if any.
select coalesce(spot_request_id, '')::text from hosts where instance_id = @instance_id;
