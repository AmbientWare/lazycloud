-- The planner sizes its targets from current load, so the forecast,
-- activation timings and consolidation state go.

drop table fleet_activations;

-- Only the forecast read these.
drop index containers_arrived;
drop index containers_ready_release;

alter table fleet_markets
    drop column pressure_since,
    drop column consolidating_host,
    drop column consolidation_started_at,
    drop column consolidation_cooldown_until;

-- Only activation timing read it; the resuming phase alone marks a
-- requested resume.
alter table hosts drop column resume_requested_at;

-- When a serving host became idle; null while it runs containers.
alter table hosts rename column light_since to idle_since;

-- A host cordoned for a consolidation takes work again.
update hosts set capacity_state = 'available', capacity_reason = ''
where phase = 'ready' and capacity_reason = 'consolidating';

-- The admin Nodes page and the agent rollout read live platform hosts by id
-- without walking the failed and deleted history, which is kept forever.
drop index hosts_platform;
create index hosts_platform on hosts (id) where kind = 'platform' and phase not in ('deleted', 'failed');
