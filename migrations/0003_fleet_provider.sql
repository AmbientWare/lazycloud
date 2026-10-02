-- Platform reserves: hosts the fleet stops and resumes instead of buying,
-- the EC2 facts each stop and start needs, live Spot prices, and when an
-- offer was last refused.

-- preparing: the agent is proving the host may stop into the reserve.
alter table hosts drop constraint hosts_phase_check;
alter table hosts add constraint hosts_phase_check check (phase in (
    'requested', 'provisioning', 'booting', 'joining', 'ready', 'draining', 'preparing', 'stopping', 'stopped',
    'resuming', 'terminating', 'deleted', 'failed'
));

-- launch_lease_until now also holds a host for the reserve actuator's
-- start, stop and terminate calls, as it holds a requested host for a
-- launcher.
alter table hosts
    -- How the host sleeps in the reserve; null while it serves.
    add column reserve_mode text check (reserve_mode in ('stop', 'hibernate')),
    -- The instance launched able to hibernate.
    add column hibernation_configured boolean not null default false,
    -- The persistent Spot request that launched a Spot reserve; it must be
    -- cancelled before the instance terminates, or EC2 relaunches it.
    add column spot_request_id text,
    -- The AMI the instance launched from.
    add column node_image text,
    -- When EC2 accepted this stop, and when a stuck stop was forced.
    add column stop_requested_at timestamptz,
    add column force_stop_at timestamptz,
    -- When EC2 first refused to hibernate this stop; refusals are retried
    -- for a while before the host stops plainly.
    add column hibernate_refused_at timestamptz,
    -- When EC2 reported the instance stopped.
    add column stopped_at timestamptz,
    -- When the planner asked a stopped reserve to start; only a requested
    -- resume serves.
    add column resume_requested_at timestamptz,
    -- Whether the last stop saved a hibernation image: unknown while a
    -- hibernation stops, saved once EC2 stopped it for the hibernation
    -- (the agent's resume report proves it), failed when EC2 stopped it
    -- otherwise, unavailable after a plain or forced stop.
    add column image_evidence text not null default 'unknown'
        check (image_evidence in ('unknown', 'saved', 'failed', 'unavailable'));

-- Hosts waiting on a start, stop or terminate call.
create index hosts_provider_actions on hosts (phase, phase_at)
    where provider = 'aws' and phase in ('stopping', 'resuming', 'terminating');

-- The latest Spot price per zone and type, refreshed by the scheduler
-- leader. A region that fails to refresh keeps its rows; readers ignore a
-- price observed too long ago.
create table spot_prices (
    region text not null,
    availability_zone_id text not null,
    instance_type text not null,
    hourly_micros bigint not null check (hourly_micros > 0),
    effective_at timestamptz not null,
    observed_at timestamptz not null,
    primary key (region, availability_zone_id, instance_type)
);

-- The platform account's EC2 vCPU quotas per region, quota class and
-- market, read hourly; vcpus is null until read. A quota refusal holds the
-- class at no room until refused_until.
create table fleet_quotas (
    region text not null,
    quota_class text not null check (quota_class in ('standard', 'g', 'p')),
    market text not null check (market in ('spot', 'on_demand')),
    vcpus integer check (vcpus >= 0),
    observed_at timestamptz,
    refused_until timestamptz,
    primary key (region, quota_class, market)
);

-- When the offer was last refused, so refusals across a region's offers
-- can be counted over a window longer than the cooldown.
alter table capacity_cooldowns add column refused_at timestamptz;
