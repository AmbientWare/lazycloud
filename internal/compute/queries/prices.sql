-- name: UpsertSpotPrices :exec
-- One region's latest Spot price per zone and type, in one statement. The
-- arrays have one element per price.
insert into spot_prices (region, availability_zone_id, instance_type, hourly_micros, effective_at, observed_at)
select @region::text, unnest(@zone_ids::text[]), unnest(@instance_types::text[]), unnest(@hourly_micros::bigint[]),
       unnest(@effective_at::timestamptz[]), now()
on conflict (region, availability_zone_id, instance_type) do update
set hourly_micros = excluded.hourly_micros, effective_at = excluded.effective_at, observed_at = excluded.observed_at;

-- name: ScoreSpotPools :exec
-- One region's Spot placement scores per zone and type, in one statement,
-- on the prices stored. The arrays have one element per score.
update spot_prices s
set placement_score = v.score, scored_at = now()
from (select unnest(@zone_ids::text[]) as zone_id, unnest(@instance_types::text[]) as instance_type,
             unnest(@scores::smallint[]) as score) v
where s.region = @region::text and s.availability_zone_id = v.zone_id and s.instance_type = v.instance_type;

-- name: FreshSpotPrices :many
-- Spot prices observed recently enough to buy on, with their placement
-- scores while those are fresh; 0 is no fresh score.
select region, availability_zone_id, instance_type, hourly_micros, effective_at, observed_at,
       coalesce(case when scored_at > now() - make_interval(secs => @score_age_seconds::float8) then placement_score end,
                0)::smallint as placement_score
from spot_prices
where observed_at > now() - make_interval(secs => @max_age_seconds::float8)
order by region, availability_zone_id, instance_type;
