-- Images

-- An image is global and content-addressed: digest covers the rendered
-- Dockerfile with every FROM pinned by digest, the architecture, the build
-- context digest and the GPU hint. reference is the pullable OCI reference by
-- digest; it is null until a build publishes it.
create table images (
    digest bytea primary key check (length(digest) = 32),
    id text not null unique check (id ~ '^img_[0-9a-f]{24}$'),
    dockerfile text not null,
    python_version text not null,
    architecture text not null check (architecture in ('amd64', 'arm64')),
    reference text,
    created_at timestamptz not null default now(),
    ready_at timestamptz,
    check ((reference is null) = (ready_at is null))
);

-- A workspace may deploy an image only after it resolved the image's
-- definition itself, which proves it holds every input.
create table workspace_images (
    workspace_id uuid not null references workspaces (id) on delete cascade,
    image_digest bytea not null references images (digest) on delete cascade,
    created_at timestamptz not null default now(),
    primary key (workspace_id, image_digest)
);

-- One build per image may be building; a concurrent request joins it.
-- registry_auth holds the Docker auth entries for private base images and is
-- cleared when the build ends. context names the source archive the build
-- reads, stored in context_workspace_id.
create table image_builds (
    id uuid primary key default uuidv7(),
    image_digest bytea not null references images (digest) on delete cascade,
    state text not null check (state in ('building', 'succeeded', 'failed')),
    context_workspace_id uuid references workspaces (id) on delete set null,
    context_sha256 bytea check (length(context_sha256) = 32),
    registry_auth jsonb,
    failure text,
    created_at timestamptz not null default now(),
    deadline_at timestamptz not null,
    finished_at timestamptz,
    check ((state = 'building') = (finished_at is null))
);

create unique index image_builds_building on image_builds (image_digest) where state = 'building';
create index image_builds_image on image_builds (image_digest, created_at);

create table image_build_logs (
    id bigint generated always as identity primary key,
    build_id uuid not null references image_builds (id) on delete cascade,
    attempt integer not null,
    data text not null,
    logged_at timestamptz not null
);

create index image_build_logs_build on image_build_logs (build_id, id);

-- A container runs either a release's workload or one image build attempt.
alter table containers
    alter column release_id drop not null,
    add column image_build_id uuid references image_builds (id) on delete cascade,
    add constraint containers_owner check ((release_id is null) <> (image_build_id is null));

create index containers_image_build on containers (image_build_id, created_at) where image_build_id is not null;
