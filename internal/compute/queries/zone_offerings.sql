-- name: UpsertZoneOfferings :exec
-- One region's zone offerings, one element per zone and type.
insert into fleet_zone_offerings (region, availability_zone_id, instance_type, observed_at)
select @region::text, unnest(@zone_ids::text[]), unnest(@instance_types::text[]), now()
on conflict (region, availability_zone_id, instance_type) do update set observed_at = excluded.observed_at;

-- name: DropStaleZoneOfferings :exec
-- After the upsert in the same transaction: what the region's read no
-- longer listed.
delete from fleet_zone_offerings where region = @region and observed_at < now();

-- name: ZoneOfferings :many
select region, availability_zone_id, array_agg(instance_type order by instance_type)::text[] as instance_types
from fleet_zone_offerings
group by region, availability_zone_id
order by region, availability_zone_id;
