-- name: LockHost :one
select id, phase, state, interruption_at from hosts where id = @id for update;

-- name: MarkInterrupted :exec
-- The host takes no new work from now on and is preempted before reclaim_at.
update hosts
set phase = case when phase in ('ready', 'joining') then 'draining' else phase end,
    phase_message = @message, phase_at = now(),
    capacity_state = 'preempting', capacity_reason = @reason,
    interruption_at = @reclaim_at, updated_at = now()
where id = @id;

-- name: DuePreemptions :many
-- Interrupted hosts whose reclaim time is near that still run containers.
select h.id from hosts h
where h.interruption_at is not null
  and h.interruption_at <= now() + make_interval(secs => @lead_seconds::float8)
  and h.capacity_state = 'preempting' and h.phase = 'draining'
  and exists (select 1 from containers c where c.host_id = h.id and c.state <> 'stopped')
order by h.interruption_at, h.id
limit @batch_size;
