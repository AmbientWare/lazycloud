-- Workloads: pods, devboxes and sandboxes, containers started on request,
-- idle leases, SSH identities and memory snapshots.

alter table workloads drop constraint workloads_kind_check;
alter table workloads add constraint workloads_kind_check
    check (kind in ('function', 'endpoint', 'asgi', 'pod', 'sandbox'));

-- exited: a pod's command ended on its own, with exit_code.
alter table containers drop constraint containers_stop_reason_check;
alter table containers add constraint containers_stop_reason_check check (stop_reason in (
    'stopped', 'load_error', 'start_failed', 'crashed', 'out_of_memory', 'host_lost', 'exited'
));

-- purpose: serve containers are the ones planning counts for a release; an
-- instance (Pod.create, Sandbox.create) or a shell container was started on
-- request and planning never counts, drains or replaces it.
--
-- active_until: the container is not idle before this time; null never
-- idles. Activity pushes it to now + keep_warm_seconds. A container is idle
-- once it passed and no container_leases row is unexpired: an idle
-- instance stops, and planning stops an idle pod container above its count.
alter table containers
    add column purpose text not null default 'serve' check (purpose in ('serve', 'instance', 'shell')),
    add column keep_warm_seconds integer check (keep_warm_seconds >= 0),
    add column active_until timestamptz,
    -- An instance's command in place of its release's.
    add column command text[],
    -- The memory snapshot the container starts from.
    add column snapshot_id uuid,
    add column block_network boolean not null default false,
    add column allow_list text[] not null default '{}',
    -- Ports exposed after the start; a release's own ports are always
    -- exposed.
    add column exposed_ports integer[] not null default '{}',
    add column exit_code integer;

create index containers_live_instances on containers (active_until)
    where purpose <> 'serve' and state <> 'stopped';

-- An open connection to a container: a shell, an SSH session, a tunnel or
-- an HTTP request in flight through an edge. The holder renews expires_at
-- while it lasts and deletes the row when it ends; a holder that dies lets
-- it expire. While any row of a container is unexpired the container is
-- not idle.
create table container_leases (
    container_id uuid not null references containers (id) on delete cascade,
    holder_id uuid not null,
    expires_at timestamptz not null,
    primary key (container_id, holder_id)
);

create index container_leases_expires on container_leases (expires_at);

-- A pod's count and wake. replicas, set by a scale, holds the pod at that
-- many containers; without it the pod runs one container while it has
-- connections or was woken in the last 15 minutes, and keeps it until it is
-- idle. A parked pod (a stopped devbox) runs none until it is woken.
create table pod_states (
    workload_id uuid primary key references workloads (id) on delete cascade,
    replicas integer check (replicas >= 0),
    woken_at timestamptz,
    parked boolean not null default false
);

-- The workspace's SSH user certificate authority and each pod's host key.
-- The private keys are sealed by the secrets owner, bound to their row, so
-- either rotates by replacing its row.
create table ssh_authorities (
    workspace_id uuid primary key references workspaces (id) on delete cascade,
    public_key text not null,
    sealed_key bytea not null,
    created_at timestamptz not null default now()
);

create table ssh_host_keys (
    workload_id uuid primary key references workloads (id) on delete cascade,
    public_key text not null,
    sealed_key bytea not null,
    created_at timestamptz not null default now()
);

-- A checkpoint of a container's memory and writable filesystem, stored at
-- workspaces/<workspace>/snapshots/<id>.tar in the platform bucket. An
-- automatic snapshot is the one a checkpoint_enabled release starts its
-- containers from; at most one per release is pending or available.
create table memory_snapshots (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    release_id uuid not null references releases (id) on delete cascade,
    container_id uuid references containers (id) on delete set null,
    automatic boolean not null default false,
    state text not null check (state in ('pending', 'available', 'failed')),
    failure text,
    size_bytes bigint,
    sha256 text check (sha256 ~ '^[0-9a-f]{64}$'),
    created_at timestamptz not null default now(),
    finished_at timestamptz,
    check ((state = 'pending') = (finished_at is null)),
    check (state <> 'available' or (size_bytes is not null and sha256 is not null))
);

create index memory_snapshots_release on memory_snapshots (release_id, created_at desc);
create unique index memory_snapshots_automatic on memory_snapshots (release_id)
    where automatic and state <> 'failed';
create index memory_snapshots_pending on memory_snapshots (created_at) where state = 'pending';

-- A filesystem image a container is publishing, until the host reports it.
create table filesystem_images (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    container_id uuid not null references containers (id) on delete cascade,
    state text not null check (state in ('publishing', 'published', 'failed')),
    image_id text,
    failure text,
    created_at timestamptz not null default now(),
    finished_at timestamptz,
    check ((state = 'publishing') = (finished_at is null)),
    check ((state = 'published') = (image_id is not null))
);

create index filesystem_images_publishing on filesystem_images (created_at) where state = 'publishing';

-- disk: the agent leased and restored the container's disks.
alter table container_startup_stages drop constraint container_startup_stages_stage_check;
alter table container_startup_stages add constraint container_startup_stages_stage_check
    check (stage in ('image', 'source', 'create', 'runtime', 'disk'));
