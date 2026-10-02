-- name: TargetRelease :one
select version, sha256_amd64, sha256_arm64, rollout_percent from agent_releases where target;

-- name: AgentRelease :one
select version, sha256_amd64, sha256_arm64, rollout_percent from agent_releases where version = @version;

-- name: UpsertAgentRelease :execrows
-- A version is immutable: republishing it with other digests changes no row.
insert into agent_releases (version, sha256_amd64, sha256_arm64, rollout_percent)
values (@version, sqlc.narg(sha256_amd64), sqlc.narg(sha256_arm64), @rollout_percent)
on conflict (version) do update
set rollout_percent = excluded.rollout_percent
where agent_releases.sha256_amd64 is not distinct from excluded.sha256_amd64
  and agent_releases.sha256_arm64 is not distinct from excluded.sha256_arm64;

-- name: ClearTargetRelease :exec
update agent_releases set target = false where target and version <> @version;

-- name: SetTargetRelease :execrows
update agent_releases set target = true where version = @version;

-- name: HostArchitecture :one
select architecture from hosts where id = @id;

-- name: MarkUpdating :exec
-- An agent told to update restarts; until this passes it is not lost.
update hosts set updating_until = now() + make_interval(secs => @seconds::float8) where id = @id;
