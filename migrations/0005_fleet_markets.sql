-- The platform fleet's published plan per market, and when each serving host
-- became lightly used.

-- One row per purchase market, written by the fleet planning pass. plan is
-- the market's targets and measures as of generated_at, current until
-- expires_at; an expired plan is no plan. pressure_since is when running
-- free room fell short of the warm target, which brings the next reserve
-- pass forward. A market drains at most one lightly used host at a time
-- (consolidating_host), then waits out its cooldown.
create table fleet_markets (
    market text primary key,
    plan jsonb not null,
    generated_at timestamptz not null,
    expires_at timestamptz not null,
    pressure_since timestamptz,
    consolidating_host uuid,
    consolidation_started_at timestamptz,
    consolidation_cooldown_until timestamptz
);

-- When a serving host last became lightly used, null while it is not:
-- retention and consolidation wait on it. It replaces idle_since, since a
-- connected account's host is lightly used only when idle.
alter table hosts add column light_since timestamptz;
alter table hosts drop column idle_since;

-- Containers that left pending, by id: the planner reads the last ten
-- minutes' arrivals as a uuidv7 range without walking a pending backlog.
create index containers_arrived on containers (id) where state <> 'pending';

-- Ready containers by release: the planner counts a scheduled function's
-- warm containers without reading its pending backlog.
create index containers_ready_release on containers (release_id) where state = 'ready';
