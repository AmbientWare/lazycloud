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

-- A purchase made at a loss, as for Spot-tolerant GPU work Spot refused
-- past its wait; its launch walk sets the margin aside as well.
alter table hosts add column margin_waived boolean not null default false;

-- When an idle serving host last ran work or held a warm slot; it leaves
-- once that is the keep window ago, while idle_since, when it last ran
-- work, decides whether a cheaper type replaces it.
alter table hosts add column free_since timestamptz;
