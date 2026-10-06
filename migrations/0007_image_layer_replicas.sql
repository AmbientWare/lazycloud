-- The copies of converted layer pairs S3 replication makes in other
-- regions. A row is a check of one pair in one region's copy: checked_at
-- is when a server last claimed the check, and confirmed_at when it found
-- both objects there. Hosts in that region read the pair from the copy only
-- once it is confirmed. A pair is written once and its id never reused, so
-- a confirmation stays true until the pair's row goes, which takes its
-- checks with it.
create table image_layer_replicas (
    layer_id uuid not null references image_layers (id) on delete cascade,
    region text not null,
    checked_at timestamptz not null,
    confirmed_at timestamptz,
    primary key (layer_id, region)
);
