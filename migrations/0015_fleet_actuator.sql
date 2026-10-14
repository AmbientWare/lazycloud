-- The claim that holds a cloud host for its EC2 calls: each claim draws a
-- new token, and every outcome write names it, so a holder whose lease
-- passed writes nothing.
alter table hosts add column action_token uuid not null default gen_random_uuid();

-- When the actuator last asked EC2 to terminate the instance, or saw it
-- ending; it asks again only once EC2 had time to list it shutting down.
alter table hosts add column terminate_requested_at timestamptz;

-- Hosts the actuator claims: launches, stops, starts, terminations and
-- drained hosts that leave.
drop index hosts_provider_actions;
create index hosts_actions on hosts (phase, phase_at)
    where provider = 'aws' and phase in ('requested', 'draining', 'stopping', 'resuming', 'terminating');

-- Why a pending container waits: a host being bought for it, or what keeps
-- the fleet from buying one.
alter table containers drop constraint containers_capacity_wait_check;
alter table containers add constraint containers_capacity_wait_check check (capacity_wait in (
    'provisioning', 'limit', 'batch', 'deferred', 'cooldown', 'quota', 'margin', 'no_offer'
));

-- Each market's held targets and the keep order decide which idle hosts
-- leave; no host carries an idle clock.
alter table hosts drop column idle_since;

-- Every release records its build; a host runs what it should when the
-- rollout does not reach it or its release is of the target's build.
delete from agent_releases where build is null;
alter table agent_releases alter column build set not null;
create or replace function on_target(bucket integer, version text) returns boolean
language sql stable
return coalesce((
    select bucket >= t.rollout_percent or coalesce(v.build = t.build, false)
    from agent_releases t
    left join agent_releases v on v.version = on_target.version
    where t.target
), true);
