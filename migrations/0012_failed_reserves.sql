-- The platform reserves that failed, by when, which the fleet planner
-- reads within its retry window so a market waits before it buys another.
create index hosts_failed_reserves on hosts (phase_at)
    where provider = 'aws' and kind = 'platform' and phase = 'failed' and reserve_mode is not null;
