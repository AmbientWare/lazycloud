-- The placed platform containers that are not builds, newest first: what
-- each market's headroom reads its recent arrivals from, so the read stays
-- within its sample however many containers wait unplaced.
create index containers_placed_arrivals on containers (id)
    where billing_owner = 'platform_fleet' and assigned_at is not null and image_build_id is null;
