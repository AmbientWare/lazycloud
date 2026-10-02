-- The catalog types EC2 offers in each availability zone of the platform's
-- regions. EC2 refuses a launch into a zone that does not offer its type,
-- and the refusal would cool the type in the whole region.
create table fleet_zone_offerings (
    region text not null,
    availability_zone_id text not null,
    instance_type text not null,
    observed_at timestamptz not null,
    primary key (region, availability_zone_id, instance_type)
);
