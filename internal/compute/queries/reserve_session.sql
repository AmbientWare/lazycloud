-- name: ReserveSessionHost :one
-- What an open session does for a reserve. A preparing host's phase_at
-- names the request: a later return starts a new one.
select phase, phase_at, reserve_mode, gpu_count, coalesce(updating_until > now(), false)::bool as updating
from hosts where id = @id;

-- name: ReserveProofHost :one
select id, phase, phase_at, boot_id, gpu_count, preflight from hosts where id = @id for update;

-- name: LiveHostContainers :one
select count(*) from containers where host_id = @id and state <> 'stopped';

-- name: AcceptReserveStop :execrows
-- The agent proved the host may stop; the actuator stops it. A newer
-- request, or any other phase, takes no row.
update hosts
set phase = 'stopping', phase_message = @message, phase_at = now(),
    sleep_attempt_id = @sleep_attempt_id, sleep_boot_id = @sleep_boot_id,
    prepared_agent_version = @prepared_agent_version, gpu_proven = @gpu_proven, updated_at = now()
where id = @id and phase = 'preparing' and phase_at = @phase_at;

-- name: ResumeHost :one
-- What a Hello settles about a host coming back from the reserve.
select phase, reserve_mode, sleep_attempt_id, sleep_boot_id
from hosts where id = @id for update;

-- name: RecordResumeOutcome :exec
-- Settles the attempt, so a repeated Hello records nothing more. The
-- agent's report is the hibernation's proof: restored memory means the
-- image was saved, and a cold boot after a saved hibernation means it was
-- not.
update hosts
set last_resume_outcome = @outcome,
    image_evidence = case
        when @outcome = 'memory_restored' then 'saved'
        when @outcome = 'cold_boot' and image_evidence = 'saved' then 'failed'
        else image_evidence
    end,
    sleep_attempt_id = null, sleep_boot_id = null, updated_at = now()
where id = @id;

-- name: EndLastStop :exec
-- A resume joined: the request and the last stop's facts end, even when
-- the actuator never recorded the start, so the next stop starts clean.
update hosts
set resume_requested_at = null, stop_requested_at = null, force_stop_at = null, hibernate_refused_at = null,
    stopped_at = null, updated_at = now()
where id = @id;
