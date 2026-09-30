-- name: InsertJoinToken :one
insert into host_join_tokens (token_hash, expires_at)
values (@token_hash, now() + make_interval(secs => @ttl_seconds::float8))
returning expires_at;

-- name: UseJoinToken :one
update host_join_tokens set used_at = now()
where token_hash = @token_hash and used_at is null and expires_at > now()
returning id;

-- name: InsertHost :one
insert into hosts (name, token_hash, state, cpu_millis, memory_bytes)
values (@name, @token_hash, 'offline', @cpu_millis, @memory_bytes)
returning id;

-- name: HostByToken :one
select id from hosts where token_hash = @token_hash and state <> 'retired';

-- name: OpenHostSession :one
-- A new epoch supersedes every earlier session of the host.
update hosts
set session_epoch = session_epoch + 1,
    state = 'online',
    cpu_millis = @cpu_millis,
    memory_bytes = @memory_bytes,
    boot_id = @boot_id,
    last_seen_at = now()
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
