-- The frames of an image's layers that a workspace's container read from
-- its start until it was ready, in the order first read: layers[i] is the
-- position of a layer in the reference's layers, frames[i] a frame of it.
-- Later starts of the reference in the workspace fetch these frames ahead
-- of the container. One trace per workspace and reference, replaced once
-- it is a day old. A reference no start used through the layer grace
-- period loses its traces when the sweep purges its use.
create table image_traces (
    reference text not null references image_reference_uses (reference) on delete cascade,
    workspace_id uuid not null references workspaces (id) on delete cascade,
    layers integer[] not null,
    frames integer[] not null,
    recorded_at timestamptz not null default now(),
    primary key (reference, workspace_id),
    check (cardinality(layers) = cardinality(frames) and cardinality(frames) between 1 and 4096)
);

create index image_traces_workspace on image_traces (workspace_id);
