-- AWS's Spot placement score of each priced pool, refreshed with its price.
-- Readers ignore a score observed too long ago.
alter table spot_prices
    add column placement_score smallint check (placement_score between 1 and 10),
    add column scored_at timestamptz;
