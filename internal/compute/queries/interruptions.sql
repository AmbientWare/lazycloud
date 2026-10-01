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
-- Interrupted hosts whose reclaim time is near and that have not been
-- preempted yet.
select id from hosts
where interruption_at is not null
  and interruption_at <= now() + make_interval(secs => @lead_seconds::float8)
  and capacity_state = 'preempting' and phase = 'draining'
order by interruption_at, id
limit @batch_size;

-- name: MarkPreempted :execrows
update hosts
set phase = 'terminating', phase_message = 'Reclaimed by the provider', phase_at = now(), updated_at = now()
where id = @id and phase = 'draining' and capacity_state = 'preempting';
