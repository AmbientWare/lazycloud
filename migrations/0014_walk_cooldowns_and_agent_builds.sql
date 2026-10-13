-- The host whose launch walk or start refused the pool while it cools:
-- every pool of one walk cools until the latest of their ends, and a
-- reserve whose start was refused waits out its own refusal.
alter table capacity_cooldowns add column refused_by uuid;
create index capacity_cooldowns_refused_by on capacity_cooldowns (refused_by) where refused_by is not null;
alter table hosts drop column walk_cooled_until;

-- The digest of a release's archives but their VERSION files: releases of
-- one build run the same host code.
alter table agent_releases add column build text;

-- Whether a host in rollout bucket bucket runs what it should on agent
-- release version: there is no target, the rollout does not reach it, or
-- version is the target or of the target's build.
create function on_target(bucket integer, version text) returns boolean
language sql stable
return coalesce((
    select bucket >= t.rollout_percent or coalesce(v.version = t.version or v.build = t.build, false)
    from agent_releases t
    left join agent_releases v on v.version = on_target.version
    where t.target
), true);

-- The hosts whose agent must move to the target release, with the archive
-- for their architecture: it can update itself, does not run what it
-- should, and did not roll back from the target. Such a host takes no work
-- until it runs the target.
create or replace view agent_updates as
select h.id as host_id, r.version, h.architecture,
       (case h.architecture when 'amd64' then r.sha256_amd64 else r.sha256_arm64 end)::text as sha256
from hosts h
join agent_releases r on r.target
where h.agent_updatable
  and not on_target(h.rollout_bucket, h.agent_version)
  and h.agent_rejected_version is distinct from r.version
  and (case h.architecture when 'amd64' then r.sha256_amd64 else r.sha256_arm64 end) is not null;
