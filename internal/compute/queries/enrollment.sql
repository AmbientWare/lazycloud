-- name: InsertJoinToken :one
insert into host_join_tokens (token_hash, expires_at, host_id)
values (@token_hash, now() + make_interval(secs => @ttl_seconds::float8), sqlc.narg(host_id)::uuid)
returning expires_at;

-- name: UseJoinToken :one
update host_join_tokens set used_at = now()
where token_hash = @token_hash and used_at is null and expires_at > now()
returning id, host_id;

-- name: InsertPlatformHost :one
insert into hosts (name, token_hash, state, kind, provider, phase, phase_message, cpu_millis, memory_bytes,
                   gpu_type, gpu_count, architecture, preflight)
values (@name, @token_hash, 'offline', 'platform', 'agent', 'joining', 'Agent joined; waiting for its first heartbeat',
        @cpu_millis, @memory_bytes, @gpu_type, @gpu_count, @architecture, @preflight)
returning id;

-- name: EnrollMachine :one
-- Binds the host token to the machine the join token was minted for. A
-- machine that already joined, or was removed, takes no second host.
update hosts
set token_hash = @token_hash,
    state = 'offline',
    phase = @phase,
    phase_message = @phase_message,
    phase_at = now(),
    failure = sqlc.narg(failure),
    cpu_millis = @cpu_millis,
    memory_bytes = @memory_bytes,
    gpu_type = @gpu_type,
    gpu_count = @gpu_count,
    architecture = @architecture,
    preflight = @preflight,
    updated_at = now()
where id = @id and kind = 'machine' and phase in ('requested', 'failed') and state <> 'retired'
returning id;

-- name: EnrollCloudHost :one
-- Issues the host token to a launched instance that proved its identity.
-- The instance id must be the one the launcher recorded.
update hosts
set token_hash = @token_hash,
    state = 'offline',
    phase = 'joining',
    phase_message = 'Agent joined; waiting for its first heartbeat',
    phase_at = now(),
    cpu_millis = @cpu_millis,
    memory_bytes = @memory_bytes,
    gpu_type = case when @gpu_type::text = '' then gpu_type else @gpu_type::text end,
    gpu_count = greatest(gpu_count, @gpu_count::int),
    architecture = @architecture,
    preflight = @preflight,
    updated_at = now()
where id = @id and provider = 'aws' and instance_id = @instance_id
  and phase in ('provisioning', 'booting') and token_hash is null
returning id;

-- name: CloudHostIdentity :one
-- What an instance's identity proof must match: its account, through the
-- connection's active authorization when it runs in a customer account.
select h.id, h.kind, h.instance_id, h.phase, h.connection_id, h.region, h.node_role_arn,
       cc.aws_account_id
from hosts h
left join cloud_connections cc on cc.id = h.connection_id
where h.id = @id;

-- name: HostByToken :one
select id from hosts where token_hash = @token_hash and state <> 'retired';

-- name: OpenHostSession :one
-- A new epoch supersedes every earlier session of the host. The first
-- session of a joining host makes it ready.
update hosts
set session_epoch = session_epoch + 1,
    state = 'online',
    cpu_millis = @cpu_millis,
    memory_bytes = @memory_bytes,
    gpu_type = @gpu_type,
    gpu_count = @gpu_count,
    boot_id = @boot_id,
    agent_version = @agent_version,
    updating_until = null,
    phase = case when phase = 'joining' then 'ready' else phase end,
    phase_message = case when phase = 'joining' then 'Ready for workloads' else phase_message end,
    phase_at = case when phase = 'joining' then now() else phase_at end,
    last_seen_at = now(),
    updated_at = now()
where id = @id and state <> 'retired'
returning session_epoch;

-- name: TouchHost :one
-- Counts 0 once a newer session or host loss replaced this one.
with touched as (
    update hosts set last_seen_at = now()
    where id = @id and session_epoch = @session_epoch and state = 'online'
    returning 1
)
select count(*) from touched;
