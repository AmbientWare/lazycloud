-- The fleet planner sizes its targets from current load alone: no demand
-- forecast, activation timings or consolidation.

drop table fleet_activations;

-- Only the forecast read these.
drop index containers_arrived;
drop index containers_ready_release;

alter table fleet_markets
    drop column pressure_since,
    drop column consolidating_host,
    drop column consolidation_started_at,
    drop column consolidation_cooldown_until;

-- When a serving host became idle, null while it runs containers; it
-- leaves once idle for the idle timeout.
alter table hosts rename column light_since to idle_since;

-- A host cordoned for a consolidation takes work again.
update hosts set capacity_state = 'available', capacity_reason = ''
where phase = 'ready' and capacity_reason = 'consolidating';

-- Live platform hosts by id: the admin Nodes page and the agent rollout
-- read them without walking failed or deleted host history, which is never
-- purged.
drop index hosts_platform;
create index hosts_platform on hosts (id) where kind = 'platform' and phase not in ('deleted', 'failed');
