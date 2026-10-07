-- AWS's Spot placement score of each priced pool, refreshed with its price.
-- Readers ignore a score observed too long ago.
alter table spot_prices
    add column placement_score smallint check (placement_score between 1 and 10),
    add column scored_at timestamptz;

-- How many refused pools a requested host's launch moved past; it bounds
-- the pools one launch tries, names each pool's client token and fences a
-- launcher whose host another one moved on.
alter table hosts add column launch_pools smallint not null default 0;

-- A capacity or price refusal cools its zone alone; '' cools the offer in
-- every zone of its region.
alter table capacity_cooldowns
    add column availability_zone_id text not null default '',
    drop constraint capacity_cooldowns_pkey,
    add primary key (connection_key, region, availability_zone_id, instance_type, market);
