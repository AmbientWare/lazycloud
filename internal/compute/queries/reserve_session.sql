-- name: PreparingHost :one
-- A host the planner is returning to the reserve. Its phase_at names the
-- request: a later return starts a new one.
select phase_at, reserve_mode, gpu_count from hosts where id = @id and phase = 'preparing';

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
select phase, session_epoch, kind, provider, reserve_mode, resume_requested_at, sleep_attempt_id, sleep_boot_id,
       image_evidence, instance_type, region, gpu_type, created_at
from hosts where id = @id for update;

-- name: RecordResumeOutcome :exec
-- Settles the attempt, so a repeated Hello records nothing more.
update hosts
set last_resume_outcome = @outcome, sleep_attempt_id = null, sleep_boot_id = null, updated_at = now()
where id = @id;

-- name: ClearResumeRequest :exec
update hosts set resume_requested_at = null, updated_at = now() where id = @id;

-- name: InsertActivation :exec
-- Seconds are measured on the database clock, which set started_at.
insert into fleet_activations (kind, instance_type, region, gpu_type, seconds, outcome)
values (@kind, @instance_type, @region, @gpu_type,
        greatest(0, extract(epoch from now() - @started_at::timestamptz))::float8, @outcome);

-- name: PruneActivations :exec
delete from fleet_activations where at < now() - interval '1 day';
