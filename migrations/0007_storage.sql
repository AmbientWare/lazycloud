-- Storage: volumes, disks, artifacts, queues and maps. PostgreSQL holds
-- metadata and authority; object stores hold the bytes.

-- The bucket that holds one workspace's volumes and disks, so a host can be
-- given credentials that reach that workspace alone.
create table workspace_buckets (
    workspace_id uuid primary key references workspaces (id) on delete cascade,
    bucket text not null unique,
    -- When the sweep last removed objects no row owns, which presigned
    -- uploads and lost hosts can write after a delete.
    orphans_checked_at timestamptz,
    created_at timestamptz not null default now()
);

-- Provider keys issued to hosts that must be revoked once they expire.
create table storage_grants (
    access_key_id text primary key,
    workspace_id uuid not null references workspaces (id) on delete cascade,
    host_id uuid not null references hosts (id) on delete cascade,
    expires_at timestamptz not null,
    created_at timestamptz not null default now()
);

create index storage_grants_expiry on storage_grants (expires_at);

-- A volume's files are the objects under volumes/<id>/ in its workspace
-- bucket. Deleting frees the name at once; a sweep removes the objects and
-- then the row.
create table volumes (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    name text not null check (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$'),
    state text not null default 'active' check (state in ('active', 'deleting')),
    size_bytes bigint not null default 0,
    size_measured_at timestamptz,
    created_at timestamptz not null default now(),
    deleted_at timestamptz,
    check ((state = 'deleting') = (deleted_at is not null))
);

create unique index volumes_active_name on volumes (workspace_id, name) where state = 'active';
create index volumes_deleting on volumes (deleted_at) where state = 'deleting';
create index volumes_measured on volumes (size_measured_at nulls first) where state = 'active';

-- A container mounts a volume. A mount locks the volume FOR SHARE and a
-- delete locks it FOR UPDATE, so a delete never misses a live mount.
create table volume_mounts (
    volume_id uuid not null references volumes (id) on delete cascade,
    container_id uuid not null references containers (id) on delete cascade,
    primary key (volume_id, container_id)
);

create index volume_mounts_container on volume_mounts (container_id);

-- A disk is a chain of published generations in its workspace bucket under
-- disks/<id>/. One container holds it at a time: holder_container_id and
-- lease_token fence every publish. The holder keeps the disk until it is
-- released, or its container stopped because the host was lost.
create table disks (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    name text not null check (name ~ '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$'),
    size_bytes bigint not null check (
        size_bytes between 1073741824 and 1099511627776 and size_bytes % 4096 = 0
    ),
    state text not null default 'active' check (state in ('active', 'deleting')),
    generation bigint not null default 0,
    stored_bytes bigint not null default 0 check (stored_bytes >= 0),
    -- Deleting the holder's container ends the lease. A lease_token left
    -- without a holder fences nothing: every check matches both.
    holder_container_id uuid references containers (id) on delete set null,
    lease_token bytea,
    released_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    deleted_at timestamptz,
    check ((state = 'deleting') = (deleted_at is not null)),
    check (holder_container_id is null or lease_token is not null)
);

create unique index disks_active_name on disks (workspace_id, name) where state = 'active';
create index disks_deleting on disks (deleted_at) where state = 'deleting';
create index disks_holder on disks (holder_container_id) where holder_container_id is not null;

create table disk_generations (
    disk_id uuid not null references disks (id) on delete cascade,
    generation bigint not null check (generation >= 1),
    parent_generation bigint not null check (parent_generation >= 0),
    manifest_key text not null,
    manifest_sha256 text not null,
    flat boolean not null,
    created_at timestamptz not null default now(),
    primary key (disk_id, generation)
);

-- An artifact's bytes are workspaces/<workspace>/artifacts/<id>. A row is
-- written before the upload and becomes visible when the upload completes;
-- expires_at starts then.
create table artifacts (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    task_id uuid references tasks (id) on delete set null,
    app_id uuid references apps (id) on delete set null,
    app_name text,
    filename text not null check (length(filename) between 1 and 255),
    content_type text not null check (length(content_type) between 1 and 255),
    size_bytes bigint not null check (size_bytes >= 0),
    state text not null check (state in ('uploading', 'stored')),
    upload_id text,
    retention_seconds bigint not null check (retention_seconds > 0),
    created_at timestamptz not null default now(),
    stored_at timestamptz,
    expires_at timestamptz,
    check ((state = 'stored') = (expires_at is not null))
);

create index artifacts_listing on artifacts (workspace_id, created_at desc, id desc) where state = 'stored';
create index artifacts_task on artifacts (task_id, created_at desc) where state = 'stored';
create index artifacts_expiry on artifacts (expires_at) where state = 'stored';
create index artifacts_abandoned on artifacts (created_at) where state = 'uploading';

-- Queues are created by their first put and live until deleted.
create table queues (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    name text not null check (length(name) between 1 and 255),
    created_at timestamptz not null default now(),
    unique (workspace_id, name)
);

-- Messages pop in id order: DELETE of the oldest unlocked row.
create table queue_messages (
    queue_id uuid not null references queues (id) on delete cascade,
    id bigint generated always as identity,
    data bytea not null,
    created_at timestamptz not null default now(),
    primary key (queue_id, id)
);

-- Maps are created by their first write. Expired entries read as missing
-- and are swept.
create table maps (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    name text not null check (length(name) between 1 and 255),
    created_at timestamptz not null default now(),
    unique (workspace_id, name)
);

-- A revision comes from one sequence, so a key deleted and written again
-- never repeats a revision a client may hold.
create sequence map_entry_revisions;

create table map_entries (
    map_id uuid not null references maps (id) on delete cascade,
    key text collate "C" not null check (length(key) between 1 and 1024),
    data bytea not null,
    revision bigint not null,
    expires_at timestamptz,
    updated_at timestamptz not null default now(),
    primary key (map_id, key)
);

create index map_entries_expiry on map_entries (expires_at) where expires_at is not null;
