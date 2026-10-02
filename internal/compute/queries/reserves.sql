-- name: ClaimProviderActions :many
-- Platform hosts waiting on a start, stop or terminate call that no other
-- actuator holds, oldest phase change first. The lease outlasts one pass
-- of calls; an actuator that dies leaves it to expire.
update hosts h
set launch_lease_until = now() + make_interval(secs => @lease_seconds::float8), updated_at = now()
where h.id in (
    select r.id from hosts r
    where r.provider = 'aws' and r.kind = 'platform' and r.instance_id is not null
      and r.phase in ('stopping', 'resuming', 'terminating')
      and (r.launch_lease_until is null or r.launch_lease_until < now())
    order by r.phase_at, r.id
    limit @batch_size
    for update skip locked
)
returning h.id, h.phase, h.region, h.instance_type, h.market, h.instance_id, h.spot_request_id, h.reserve_mode,
          h.hibernation_configured, h.stop_requested_at, h.force_stop_at, h.hibernate_refused_at;

-- name: RecordStopRequested :execrows
-- EC2 accepted a stop of a stopping host: a hibernation leaves its image
-- unknown until proven, a plain or forced stop saves none.
update hosts
set stop_requested_at = coalesce(stop_requested_at, now()),
    force_stop_at = case when @forced::bool then now() else force_stop_at end,
    image_evidence = @image_evidence, updated_at = now()
where id = @id and phase = 'stopping';

-- name: RecordHibernateRefused :execrows
-- EC2 refused to hibernate a stopping host; the first refusal starts the
-- window after which it stops plainly.
update hosts
set hibernate_refused_at = coalesce(hibernate_refused_at, now()), updated_at = now()
where id = @id and phase = 'stopping';

-- name: MarkReserveStopped :execrows
-- EC2 reports a stopping host's instance stopped.
update hosts
set phase = 'stopped', phase_message = 'Stopped in the reserve', phase_at = now(), stopped_at = now(),
    launch_lease_until = null, updated_at = now()
where id = @id and phase = 'stopping';

-- name: RecordStartRequested :execrows
-- EC2 accepted the start of a resuming host, or reports it starting: the
-- last stop's facts end; its image evidence stays for the resume report.
update hosts
set stop_requested_at = null, force_stop_at = null, hibernate_refused_at = null, stopped_at = null,
    updated_at = now()
where id = @id and phase = 'resuming';

-- name: RefuseResume :execrows
-- EC2 has no capacity to start a resuming host: it retires, and a purchase
-- replaces it.
update hosts
set phase = 'terminating', phase_message = @message, phase_at = now(), state = 'retired', token_hash = null,
    launch_lease_until = null, updated_at = now()
where id = @id and phase = 'resuming';

-- name: SpotRequestOfInstance :one
-- The persistent Spot request that launched an instance, if any.
select coalesce(spot_request_id, '')::text from hosts where instance_id = @instance_id;
