-- name: UpsertSpotPrices :exec
-- One region's latest Spot price per zone and type, in one statement. The
-- arrays have one element per price.
insert into spot_prices (region, availability_zone_id, instance_type, hourly_micros, effective_at, observed_at)
select @region::text, unnest(@zone_ids::text[]), unnest(@instance_types::text[]), unnest(@hourly_micros::bigint[]),
       unnest(@effective_at::timestamptz[]), now()
on conflict (region, availability_zone_id, instance_type) do update
set hourly_micros = excluded.hourly_micros, effective_at = excluded.effective_at, observed_at = excluded.observed_at;

-- name: FreshSpotPrices :many
-- Spot prices observed recently enough to buy on.
select region, availability_zone_id, instance_type, hourly_micros, effective_at, observed_at
from spot_prices
where observed_at > now() - make_interval(secs => @max_age_seconds::float8)
order by region, availability_zone_id, instance_type;
