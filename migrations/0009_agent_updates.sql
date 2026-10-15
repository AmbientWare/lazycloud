-- What the agent's last Hello said about its release: whether it runs under
-- its service and can update itself, and the release it rolled back from.
-- rollout_bucket places the host in 0-99, stable across releases, so a
-- percentage rollout reaches the same hosts first each time.
alter table hosts
    add column agent_updatable boolean not null default false,
    add column agent_rejected_version text,
    add column rollout_bucket integer not null generated always as (
        (get_byte(sha256(uuid_send(id)), 0) * 256 + get_byte(sha256(uuid_send(id)), 1)) % 100
    ) stored;

-- The hosts whose agent must move to the target release, with the archive
-- for their architecture: it can update itself, runs another release, did
-- not roll back from the target, and the rollout reaches it. Such a host
-- takes no work until it runs the target.
create view agent_updates as
select h.id as host_id, r.version, h.architecture,
       (case h.architecture when 'amd64' then r.sha256_amd64 else r.sha256_arm64 end)::text as sha256
from hosts h
join agent_releases r on r.target
where h.agent_updatable
  and h.agent_version <> r.version
  and h.agent_rejected_version is distinct from r.version
  and h.rollout_bucket < r.rollout_percent
  and (case h.architecture when 'amd64' then r.sha256_amd64 else r.sha256_arm64 end) is not null;
