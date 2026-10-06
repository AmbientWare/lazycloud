-- Lazy images. Every layer of a published image is stored converted, as an
-- index and a framed data object (internal/imagefs) under layers/<id>/ in
-- the layers bucket. These tables are the authority for which pairs exist
-- and which images use them.

-- A converted layer pair. blob_digest is the compressed layer it was
-- converted from: the registry holds an image's blobs by digest, so the pair
-- serves any image whose manifest names that blob. diff_id is the digest the
-- converting host computed, checked against the image config.
--
-- workspace_id is set when a host the workspace controls converted the
-- layer; that pair serves only the workspace's own images. A pair a platform
-- host or the server converted serves every image.
--
-- unreferenced_since is when the sweep first found no live image using the
-- pair; it deletes the pair a grace period later.
create table image_layers (
    id uuid primary key,
    blob_digest text not null check (blob_digest ~ '^sha256:[0-9a-f]{64}$'),
    diff_id text not null check (diff_id ~ '^sha256:[0-9a-f]{64}$'),
    workspace_id uuid,
    index_bytes bigint not null check (index_bytes > 0),
    data_bytes bigint not null check (data_bytes >= 0),
    entries integer not null check (entries >= 0),
    frames integer not null check (frames >= 0),
    created_at timestamptz not null default now(),
    unreferenced_since timestamptz
);

-- One pair per blob and trust scope, so conversions of the same layer at
-- once end with one row.
create unique index image_layers_shared on image_layers (blob_digest) where workspace_id is null;
create unique index image_layers_workspace on image_layers (blob_digest, workspace_id) where workspace_id is not null;
create index image_layers_unreferenced on image_layers (unreferenced_since) where unreferenced_since is not null;

-- The layers of each converted reference (registry/repository@digest), in
-- the image's order. A reference has rows only once every layer is
-- converted.
create table image_reference_layers (
    reference text not null,
    position integer not null check (position >= 0),
    layer_id uuid not null references image_layers (id),
    primary key (reference, position)
);

create index image_reference_layers_layer on image_reference_layers (layer_id);

-- Pairs that may be in the store with no image_layers row: uploads handed to
-- a build container, a conversion that lost to another of the same blob, and
-- pairs the sweep took out of image_layers. Once expires_at passes the sweep
-- aborts the data upload, deletes the objects, then the row. container_id is
-- null for a swept pair.
--
-- upload_id is the multipart upload of the data object once the converter
-- reported the converted sizes; every part and the index are signed for
-- those sizes, so no more is stored than was reported.
create table image_layer_uploads (
    id uuid primary key default uuidv7(),
    container_id uuid,
    blob_digest text not null check (blob_digest ~ '^sha256:[0-9a-f]{64}$'),
    upload_id text,
    data_bytes bigint check (data_bytes >= 0),
    index_bytes bigint check (index_bytes > 0),
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    check ((upload_id is null) = (data_bytes is null) and (upload_id is null) = (index_bytes is null))
);

create unique index image_layer_uploads_container on image_layer_uploads (container_id, blob_digest);
create index image_layer_uploads_expiry on image_layer_uploads (expires_at);

-- The copies of converted pairs that S3 replication makes in other regions.
-- A row is a check of one pair in one region's copy: checked_at is when a
-- server last claimed the check, and confirmed_at when it found both objects
-- there. Hosts in that region read the pair from the copy only once it is
-- confirmed. A pair is written once and its id never reused, so a
-- confirmation holds until the pair's row goes, which takes its checks with
-- it.
create table image_layer_replicas (
    layer_id uuid not null references image_layers (id) on delete cascade,
    region text not null,
    checked_at timestamptz not null,
    confirmed_at timestamptz,
    primary key (layer_id, region)
);

-- When each reference was last published or sent to a host in a start. A
-- reference used within the layer grace period is live even if no live
-- release pins it; the sweep deletes older rows.
create table image_reference_uses (
    reference text primary key,
    used_at timestamptz not null
);

create index image_reference_uses_used on image_reference_uses (used_at);

-- The frames of an image's layers that a workspace's container read from
-- its start until it was ready, in the order first read: layers[i] is the
-- position of a layer in the reference's layers, frames[i] a frame of it.
-- Later starts of the reference in the workspace fetch these frames ahead
-- of the container. One trace per workspace and reference, replaced once
-- it is a day old. The sweep's purge of a reference's use drops its traces.
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

-- A mirror build has no steps: it copies an image into the platform registry
-- to convert it, and runs on a platform host whatever its workspace's hosts,
-- so its pairs serve every workspace. failure_transient marks a failure that
-- says nothing of the image, such as a lost container or an unreachable
-- store; the next start that needs the image builds it again at once.
alter table image_builds
    add column mirror boolean not null default false,
    add column failure_transient boolean not null default false;

-- The image reference a container was started with, recorded as its start
-- is sent. Its host may read that reference's converted layers while the
-- container is live; layer grant refreshes list them from here.
alter table containers add column image_reference text;

-- The images the server converts itself for each host architecture: the
-- images agents run on their own (the image builder, the volume mount image)
-- and the managed images. reference is the public image by digest; mirror is
-- its copy for that architecture in the platform registry, whose converted
-- layers are rows of image_reference_layers like any reference's.
--
-- A conversion holds the lease (lease_token until leased_until), so one
-- server replica converts a reference at a time; the record of its result
-- names the token, so an owner whose lease lapsed records nothing. failure
-- is the last attempt's, and failure_transient marks one that says nothing
-- of the image, such as an unreachable registry or store.
create table platform_images (
    reference text not null,
    architecture text not null check (architecture in ('amd64', 'arm64')),
    mirror text,
    lease_token uuid,
    leased_until timestamptz,
    failure text,
    failure_transient boolean not null default false,
    failed_at timestamptz,
    created_at timestamptz not null default now(),
    primary key (reference, architecture)
);

-- The image a container without one of its own runs: the server's managed
-- template for a Python version, pinned once to source, the template's image
-- by digest, and converted as a platform image. Another template pins
-- another source.
create table managed_images (
    python_version text not null,
    template text not null,
    source text not null,
    created_at timestamptz not null default now(),
    primary key (python_version, template)
);
