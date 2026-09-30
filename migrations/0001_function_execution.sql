-- Identity

create table users (
    id uuid primary key default uuidv7(),
    email text not null unique,
    is_admin boolean not null default false,
    created_at timestamptz not null default now()
);

create table workspaces (
    id uuid primary key default uuidv7(),
    name text not null unique check (name ~ '^[a-z][a-z0-9-]{0,62}$'),
    created_at timestamptz not null default now()
);

create table workspace_members (
    workspace_id uuid not null references workspaces (id) on delete cascade,
    user_id uuid not null references users (id) on delete cascade,
    role text not null check (role in ('owner', 'member')),
    created_at timestamptz not null default now(),
    primary key (workspace_id, user_id)
);

create index workspace_members_user on workspace_members (user_id);

-- A token restricted to one workspace has workspace_id set.
create table api_tokens (
    id uuid primary key default uuidv7(),
    user_id uuid not null references users (id) on delete cascade,
    workspace_id uuid references workspaces (id) on delete cascade,
    name text not null,
    token_hash bytea not null unique,
    created_at timestamptz not null default now(),
    expires_at timestamptz,
    revoked_at timestamptz
);

-- Compute

create table host_join_tokens (
    id uuid primary key default uuidv7(),
    token_hash bytea not null unique,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    used_at timestamptz
);

-- session_epoch increases with every Hello; a server holding an older epoch
-- has lost the session and stops acting for the host.
create table hosts (
    id uuid primary key default uuidv7(),
    name text not null,
    token_hash bytea not null unique,
    state text not null check (state in ('online', 'offline', 'lost', 'retired')),
    cpu_millis bigint not null check (cpu_millis >= 0),
    memory_bytes bigint not null check (memory_bytes >= 0),
    boot_id text not null default '',
    session_epoch bigint not null default 0,
    last_seen_at timestamptz,
    created_at timestamptz not null default now()
);

create index hosts_online on hosts (last_seen_at) where state = 'online';

-- Storage

create table source_objects (
    workspace_id uuid not null references workspaces (id) on delete cascade,
    sha256 bytea not null check (length(sha256) = 32),
    size_bytes bigint not null check (size_bytes > 0),
    created_at timestamptz not null default now(),
    primary key (workspace_id, sha256)
);

-- Control

create table apps (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    name text not null check (name ~ '^[a-z][a-z0-9_]{0,62}$'),
    state text not null check (state in ('active', 'paused')),
    created_at timestamptz not null default now(),
    unique (workspace_id, name)
);

create table workloads (
    id uuid primary key default uuidv7(),
    app_id uuid not null references apps (id) on delete cascade,
    kind text not null check (kind in ('function')),
    name text not null,
    desired_state text not null check (desired_state in ('active', 'stopped')),
    active_release_id uuid,
    next_version integer not null default 1,
    created_at timestamptz not null default now(),
    unique (app_id, kind, name)
);

create table releases (
    id uuid primary key default uuidv7(),
    workload_id uuid not null references workloads (id) on delete cascade,
    version integer not null,
    spec jsonb not null,
    spec_digest bytea not null check (length(spec_digest) = 32),
    source_sha256 bytea not null check (length(source_sha256) = 32),
    -- Consecutive start_failed containers since the last ready one.
    start_failures integer not null default 0,
    created_at timestamptz not null default now(),
    unique (workload_id, version)
);

alter table workloads
    add foreign key (active_release_id) references releases (id);

-- Execution

create table tasks (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    workload_id uuid not null references workloads (id) on delete cascade,
    release_id uuid not null references releases (id) on delete cascade,
    status text not null check (status in ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    attempt_count integer not null default 0,
    max_attempts integer not null check (max_attempts >= 1),
    available_at timestamptz not null default now(),
    current_attempt_id uuid,
    failure jsonb,
    created_at timestamptz not null default now(),
    started_at timestamptz,
    finished_at timestamptz
);

create index tasks_queued on tasks (release_id, available_at, id) where status = 'queued';
create index tasks_running on tasks (release_id) where status = 'running';
create index tasks_workload_queued on tasks (workload_id) where status = 'queued';

create table task_inputs (
    task_id uuid primary key references tasks (id) on delete cascade,
    encoding text not null check (encoding in ('json', 'cloudpickle')),
    data bytea not null
);

create table task_results (
    task_id uuid primary key references tasks (id) on delete cascade,
    encoding text not null check (encoding in ('json', 'cloudpickle')),
    data bytea not null
);

-- A container row is placed at most once; host_id never changes after
-- assignment, so the container id fences stale hosts.
create table containers (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    release_id uuid not null references releases (id) on delete cascade,
    state text not null check (state in ('pending', 'starting', 'ready', 'draining', 'stopped')),
    host_id uuid references hosts (id),
    slots integer not null check (slots >= 1),
    cpu_millis bigint not null,
    memory_bytes bigint not null,
    stop_reason text check (stop_reason in (
        'stopped', 'load_error', 'start_failed', 'crashed', 'out_of_memory', 'host_lost'
    )),
    exit_message text,
    created_at timestamptz not null default now(),
    assigned_at timestamptz,
    ready_at timestamptz,
    drain_started_at timestamptz,
    stopped_at timestamptz,
    check ((state = 'pending') = (host_id is null) or state = 'stopped')
);

create index containers_live_release on containers (release_id) where state <> 'stopped';
create index containers_live_host on containers (host_id) where state <> 'stopped';
create index containers_pending on containers (created_at) where state = 'pending';

create table attempts (
    id uuid primary key default uuidv7(),
    task_id uuid not null references tasks (id) on delete cascade,
    number integer not null,
    container_id uuid not null references containers (id) on delete cascade,
    state text not null check (state in ('running', 'succeeded', 'failed', 'timed_out', 'cancelled', 'lost')),
    started_at timestamptz not null default now(),
    deadline_at timestamptz not null,
    finished_at timestamptz,
    unique (task_id, number)
);

create index attempts_running_deadline on attempts (deadline_at) where state = 'running';
create index attempts_running_container on attempts (container_id) where state = 'running';
create index attempts_container_finished on attempts (container_id, finished_at);

alter table tasks
    add foreign key (current_attempt_id) references attempts (id);

create table task_logs (
    id bigint generated always as identity primary key,
    task_id uuid not null references tasks (id) on delete cascade,
    attempt integer not null,
    stream text not null check (stream in ('stdout', 'stderr', 'system')),
    data text not null,
    logged_at timestamptz not null
);

create index task_logs_task on task_logs (task_id, id);
