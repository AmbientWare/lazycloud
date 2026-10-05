-- Lazy images. Every layer of a published image is stored converted, as an
-- index and a framed data object (internal/imagefs) under layers/<id>/ in
-- the platform bucket. These tables are the authority for which pairs exist
-- and which images use them.

-- A converted layer pair. blob_digest is the compressed layer it was
-- converted from: the registry holds an image's blobs by digest, so the pair
-- serves any image whose manifest names that blob. diff_id is the digest the
-- converting host computed, checked against the image config.
--
-- workspace_id is set when a host the workspace controls converted the
-- layer; that pair serves only the workspace's own images. A pair a platform
-- host converted serves every image.
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

-- One pair per blob and trust scope, so builds converting the same layer at
-- once end with one row.
create unique index image_layers_shared on image_layers (blob_digest) where workspace_id is null;
create unique index image_layers_workspace on image_layers (blob_digest, workspace_id) where workspace_id is not null;
create index image_layers_unreferenced on image_layers (unreferenced_since) where unreferenced_since is not null;

-- The layers of each published reference (registry/repository@digest), in
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
-- upload_id is the multipart upload of the data object once the host
-- reported the converted sizes; every part and the index are signed for
-- those sizes, so the host stores no more than it reported.
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

-- When each reference was last published or sent to a host in a start. A
-- reference used within the layer grace period is live even if no live
-- release pins it; the sweep deletes older rows.
create table image_reference_uses (
    reference text primary key,
    used_at timestamptz not null
);

create index image_reference_uses_used on image_reference_uses (used_at);

-- A mirror build has no steps: it copies an image into the platform registry
-- to convert it, and runs on a platform host whatever its workspace's hosts,
-- so its pairs serve every workspace. failure_transient marks a failure that
-- says nothing of the image, such as a lost container or an unreachable
-- store; the next start that needs the image builds it again at once.
alter table image_builds
    add column mirror boolean not null default false,
    add column failure_transient boolean not null default false;

-- The image a container without one of its own runs: the server's managed
-- template for a Python version, mirrored into the platform registry and
-- converted by a build with no steps. One per version, template and host
-- architecture; another template makes another image.
create table managed_images (
    python_version text not null,
    template text not null,
    architecture text not null check (architecture in ('amd64', 'arm64')),
    image_digest bytea not null references images (digest) on delete cascade,
    created_at timestamptz not null default now(),
    primary key (python_version, template, architecture)
);
