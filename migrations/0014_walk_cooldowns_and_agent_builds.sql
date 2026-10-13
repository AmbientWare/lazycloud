-- The requested host whose launch walk refused the pool first while it
-- cools; every pool of one walk cools until the latest of their ends.
alter table capacity_cooldowns add column walk uuid;
create index capacity_cooldowns_walk on capacity_cooldowns (walk) where walk is not null;
alter table hosts drop column walk_cooled_until;

-- When EC2 last refused to start the reserve for capacity; it does not
-- resume again within the capacity cooldown.
alter table hosts add column start_refused_at timestamptz;

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
