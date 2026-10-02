-- The LazyCloud schema, by owner. Columns keep the order the owners' queries
-- and generated models read. Foreign keys that point at a later owner's table
-- are added once that table exists.

-- Notifications

-- One transactional email. state follows the message: queued until the
-- provider accepts it, then sent and whatever the provider reports, or failed
-- when delivery gave up, or discarded when its content stopped being true.
-- attempts fences a delivery: a settle names the attempt it claimed.
create table email_outbox (
    id uuid primary key default uuidv7(),
    recipient text not null,
    subject text not null,
    html text not null,
    body_text text not null,
    state text not null default 'queued' check (state in (
        'queued', 'sent', 'delivered', 'bounced', 'complained', 'failed', 'discarded'
    )),
    attempts integer not null default 0,
    next_attempt_at timestamptz not null default now(),
    lease_until timestamptz,
    provider_message_id text unique,
    last_error text not null default '',
    delivery_event_at timestamptz,
    created_at timestamptz not null default now(),
    settled_at timestamptz,
    purged_at timestamptz
);

create index email_outbox_due on email_outbox (next_attempt_at) where state = 'queued';
create index email_outbox_purge on email_outbox (settled_at) where purged_at is null and settled_at is not null;

-- Identity: accounts, sign-in, sessions, device codes, tokens, workspace
-- lifecycle, roles and invitations.

-- An account signs in with GitHub once github_user_id is set. Accounts made
-- by the admin command have an email and no GitHub identity until their first
-- sign-in with that verified email.
create table users (
    id uuid primary key default uuidv7(),
    email text unique,
    is_admin boolean not null default false,
    created_at timestamptz not null default now(),
    display_name text not null default '',
    avatar_url text not null default '',
    github_user_id bigint unique,
    github_login text not null default '',
    status text not null default 'active' check (status in ('active', 'disabled')),
    updated_at timestamptz not null default now()
);

-- A deleting workspace refuses every request except its deletion, which the
-- scheduler finishes by removing the row. A workspace lives on the platform's
-- compute (null connection_id) or in one connected account, fixed at
-- creation.
create table workspaces (
    id uuid primary key default uuidv7(),
    name text not null unique check (name ~ '^[a-z][a-z0-9-]{0,62}$'),
    created_at timestamptz not null default now(),
    state text not null default 'active' check (state in ('active', 'deleting')),
    deletion_requested_at timestamptz,
    updated_at timestamptz not null default now(),
    connection_id uuid
);

create index workspaces_deleting on workspaces (deletion_requested_at) where state = 'deleting';
create index workspaces_connection on workspaces (connection_id) where connection_id is not null;

create table workspace_members (
    workspace_id uuid not null references workspaces (id) on delete cascade,
    user_id uuid not null references users (id) on delete cascade,
    role text not null check (role in ('owner', 'administrator', 'member')),
    created_at timestamptz not null default now(),
    primary key (workspace_id, user_id)
);

create index workspace_members_user on workspace_members (user_id);
-- One owner per workspace; ownership is never offered or changed by a role
-- update.
create unique index workspace_members_one_owner on workspace_members (workspace_id) where role = 'owner';
create index workspace_members_owned on workspace_members (user_id, created_at) where role = 'owner';

-- A token restricted to one workspace has workspace_id set. device marks
-- tokens minted by device-code login; last_used_at is written in batches, so
-- it lags by up to the flush interval. prefix is the first characters of the
-- token, kept so a person can tell their tokens apart in a list.
create table api_tokens (
    id uuid primary key default uuidv7(),
    user_id uuid not null references users (id) on delete cascade,
    workspace_id uuid references workspaces (id) on delete cascade,
    name text not null,
    token_hash bytea not null unique,
    created_at timestamptz not null default now(),
    expires_at timestamptz,
    revoked_at timestamptz,
    device boolean not null default false,
    last_used_at timestamptz,
    prefix text not null default ''
);

create index api_tokens_user on api_tokens (user_id, id) where revoked_at is null;

-- A browser session; the cookie holds the token and the row its digest.
create table sessions (
    id uuid primary key default uuidv7(),
    user_id uuid not null references users (id) on delete cascade,
    token_hash bytea not null unique,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null
);

create index sessions_expiry on sessions (expires_at);

-- A device-code login. The device code is held by the CLI and stored as its
-- digest; the user code is what the person types at /activate.
create table device_codes (
    id uuid primary key default uuidv7(),
    device_code_hash bytea not null unique,
    user_code text not null unique check (user_code ~ '^[A-Z]{4}-[A-Z]{4}$'),
    client_name text not null,
    status text not null check (status in ('pending', 'approved', 'denied')),
    user_id uuid references users (id) on delete cascade,
    poll_interval_seconds integer not null,
    last_polled_at timestamptz,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    decided_at timestamptz,
    consumed_at timestamptz,
    check ((status = 'approved') = (user_id is not null))
);

create index device_codes_expiry on device_codes (expires_at);

-- An open offer of membership. Accepting, declining and revoking delete it;
-- resending replaces token_hash, so the earlier link stops working.
create table invitations (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    email text not null check (email = lower(email)),
    role text not null check (role in ('administrator', 'member')),
    invited_by uuid references users (id) on delete set null,
    token_hash bytea not null unique,
    message_id uuid references email_outbox (id) on delete set null,
    expires_at timestamptz not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (workspace_id, email)
);

-- Compute: AWS account connections, joined machines, the managed fleet,
-- interruptions and agent releases.

-- One AWS account connection per account (user). The connection's phase is
-- what users see; authorizations are its role generations.
create table cloud_connections (
    id uuid primary key default uuidv7(),
    account_id uuid not null unique references users (id) on delete cascade,
    aws_account_id text not null check (aws_account_id ~ '^[0-9]{12}$'),
    phase text not null check (phase in (
        'awaiting_authorization', 'validating', 'ready', 'degraded', 'reconnect_pending',
        'retiring_authorization', 'disconnect_draining', 'revoking', 'verifying_revocation',
        'action_required'
    )),
    -- Bumped by every phase change; clients compare it to spot a newer state.
    revision integer not null default 1,
    -- The background step a phase waits on runs at or after next_step_at.
    next_step_at timestamptz,
    step_attempts integer not null default 0,
    -- action_required: what the customer must do in AWS.
    action_url text,
    action_label text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index cloud_connections_due on cloud_connections (next_step_at) where next_step_at is not null;

alter table workspaces add foreign key (connection_id) references cloud_connections (id);

-- A role the platform assumes in the customer account. A managed_stack
-- authorization is created by the CloudFormation stack the customer submits;
-- an existing_role one names a role and networks the customer made. The
-- external ID is not a secret (it prevents confused-deputy use of the role),
-- but it is never shown again after the stack or role is created.
create table cloud_authorizations (
    id uuid primary key default uuidv7(),
    connection_id uuid not null references cloud_connections (id) on delete cascade,
    generation integer not null check (generation >= 1),
    mode text not null check (mode in ('managed_stack', 'existing_role')),
    slot text check (slot in ('active', 'pending', 'retiring')),
    phase text not null check (phase in (
        'awaiting_authorization', 'validating', 'ready', 'degraded', 'retiring', 'retired'
    )),
    role_arn text not null,
    external_id text not null,
    region text not null,
    stack_name text,
    stack_id text,
    template_version text,
    template_sha256 text,
    -- The role and instance profile the account's instances run as.
    node_role_arn text,
    node_instance_profile text,
    -- {region: {vpc_id, subnet_ids, security_group_id}} the launcher uses.
    networks jsonb not null default '{}',
    error_code text check (error_code in (
        'assume_role_denied', 'external_id_not_enforced', 'account_mismatch', 'permission_drift',
        'stack_drift', 'upstream_unavailable'
    )),
    error_message text,
    last_validation_started_at timestamptz,
    last_validated_at timestamptz,
    -- An unfinished setup lapses after a day.
    expires_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (connection_id, generation)
);

create unique index cloud_authorizations_slot on cloud_authorizations (connection_id, slot) where slot is not null;

-- Hosts. state is the session authority (online, offline, lost, retired)
-- that liveness and assignment fence on; phase is the machine lifecycle users
-- see. kind says whose capacity it is: the platform's, a connected account's
-- or an account's joined machine.
create table hosts (
    id uuid primary key default uuidv7(),
    name text not null,
    token_hash bytea unique,
    state text not null check (state in ('online', 'offline', 'lost', 'retired')),
    cpu_millis bigint not null check (cpu_millis >= 0),
    memory_bytes bigint not null check (memory_bytes >= 0),
    boot_id text not null default '',
    -- Increases with every Hello; a server holding an older epoch has lost
    -- the session and stops acting for the host.
    session_epoch bigint not null default 0,
    last_seen_at timestamptz,
    created_at timestamptz not null default now(),
    kind text not null default 'platform' check (kind in ('platform', 'connection', 'machine')),
    provider text not null default 'agent' check (provider in ('agent', 'aws')),
    -- A connection host keeps its row after the connection is removed.
    connection_id uuid references cloud_connections (id) on delete set null,
    -- The account that joined a machine.
    account_id uuid references users (id) on delete set null,
    phase text not null default 'ready' check (phase in (
        'requested', 'provisioning', 'booting', 'joining', 'ready', 'draining', 'stopping', 'stopped',
        'resuming', 'terminating', 'deleted', 'failed'
    )),
    phase_message text not null default '',
    phase_at timestamptz not null default now(),
    failure text check (failure in (
        'agent_download_failed', 'runtime_install_failed', 'network_join_failed', 'provider_identity_failed',
        'agent_enrollment_failed', 'worker_image_pull_failed', 'worker_start_failed',
        'worker_readiness_failed', 'bootstrap_timed_out', 'host_preflight_failed', 'service_lost',
        'machine_record_deleted', 'provider_stopped', 'provider_terminated', 'unknown'
    )),
    capacity_state text not null default 'available' check (capacity_state in (
        'available', 'draining', 'preempting', 'cordoned'
    )),
    capacity_reason text not null default '',
    gpu_type text not null default '',
    gpu_count integer not null default 0 check (gpu_count >= 0),
    architecture text not null default 'amd64' check (architecture in ('amd64', 'arm64')),
    agent_version text not null default '',
    -- Checks a joining host reported, [{name, ok, message, severity, remediation}].
    preflight jsonb not null default '[]',
    -- Cloud instances: the offer launched and what the provider reported.
    region text not null default '',
    availability_zone text not null default '',
    availability_zone_id text not null default '',
    instance_type text not null default '',
    market text check (market in ('spot', 'on_demand')),
    hourly_micros bigint,
    instance_id text unique,
    launch_attempts integer not null default 0,
    -- A launcher holds a requested host until this passes.
    launch_lease_until timestamptz,
    launched_at timestamptz,
    -- Set while a ready cloud host has no live container.
    idle_since timestamptz,
    -- The provider will reclaim the instance at this time.
    interruption_at timestamptz,
    -- The authorization and node role a connection host launched with: its
    -- identity proof must name that role, and a replaced authorization's
    -- stack stays until its hosts are gone.
    authorization_id uuid references cloud_authorizations (id) on delete set null,
    node_role_arn text,
    -- An agent restarting into a new release stays unlost until this passes.
    updating_until timestamptz,
    updated_at timestamptz not null default now()
);

create index hosts_online on hosts (last_seen_at) where state = 'online';
-- A machine name is unique in its account among machines that still exist.
create unique index hosts_machine_name on hosts (account_id, name) where kind = 'machine' and phase <> 'deleted';
-- Cloud hosts the fleet loops act on: neither deleted nor failed.
create index hosts_fleet on hosts (kind, phase) where provider = 'aws' and phase not in ('deleted', 'failed');

-- The workspaces a joined machine serves.
create table host_workspaces (
    host_id uuid not null references hosts (id) on delete cascade,
    workspace_id uuid not null references workspaces (id) on delete cascade,
    primary key (host_id, workspace_id)
);

create index host_workspaces_workspace on host_workspaces (workspace_id);

-- A join token enrolls one host. Platform tokens (from the admin command)
-- create a platform host; machine tokens are bound to the requested machine
-- and a newer token replaces earlier unused ones.
create table host_join_tokens (
    id uuid primary key default uuidv7(),
    token_hash bytea not null unique,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    used_at timestamptz,
    host_id uuid references hosts (id) on delete cascade
);

create index host_join_tokens_host on host_join_tokens (host_id) where used_at is null;

-- An offer that lacked capacity is skipped until the cooldown ends.
create table capacity_cooldowns (
    connection_key text not null,
    region text not null,
    instance_type text not null,
    market text not null check (market in ('spot', 'on_demand')),
    until timestamptz not null,
    reason text not null,
    primary key (connection_key, region, instance_type, market)
);

-- The GPUs a release's container reserves: gpu_count, or one when it names
-- models without a count.
create function release_gpus(spec jsonb) returns integer
language sql immutable
return greatest(
    coalesce((spec -> 'resources' ->> 'gpu_count')::integer, 0),
    case when jsonb_array_length(coalesce(spec -> 'resources' -> 'gpu', '[]'::jsonb)) > 0 then 1 else 0 end
);

-- Agent releases. One row is the target every updatable agent moves to.
create table agent_releases (
    version text primary key check (version ~ '^[A-Za-z0-9._-]{1,64}$'),
    sha256_amd64 text check (sha256_amd64 ~ '^[0-9a-f]{64}$'),
    sha256_arm64 text check (sha256_arm64 ~ '^[0-9a-f]{64}$'),
    target boolean not null default false,
    -- The share of updatable hosts, by host id, the target reaches.
    rollout_percent integer not null default 100 check (rollout_percent between 0 and 100),
    created_at timestamptz not null default now(),
    check (sha256_amd64 is not null or sha256_arm64 is not null)
);

create unique index agent_releases_target on agent_releases (target) where target;

-- Control: apps, workloads, releases and previews.

-- A deleted app or workload keeps its rows for task history and frees its
-- name; execution retires its releases.
create table apps (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    name text not null check (name ~ '^[a-z][a-z0-9_]{0,62}$'),
    state text not null check (state in ('active', 'paused', 'deleted')),
    created_at timestamptz not null default now(),
    deleted_at timestamptz
);

create unique index apps_live_name on apps (workspace_id, name) where state <> 'deleted';

create table workloads (
    id uuid primary key default uuidv7(),
    app_id uuid not null references apps (id) on delete cascade,
    kind text not null check (kind in ('function', 'endpoint', 'asgi', 'pod', 'sandbox')),
    name text not null,
    desired_state text not null check (desired_state in ('active', 'stopped', 'deleted')),
    active_release_id uuid,
    next_version integer not null default 1,
    created_at timestamptz not null default now(),
    deleted_at timestamptz
);

create unique index workloads_live_name on workloads (app_id, kind, name) where desired_state <> 'deleted';

-- A release without a version runs working-tree calls and is never active.
create table releases (
    id uuid primary key default uuidv7(),
    workload_id uuid not null references workloads (id) on delete cascade,
    version integer,
    spec jsonb not null,
    spec_digest bytea not null check (length(spec_digest) = 32),
    source_sha256 bytea not null check (length(source_sha256) = 32),
    -- Consecutive start_failed containers since the last ready one.
    start_failures integer not null default 0,
    created_at timestamptz not null default now(),
    -- The handler's import error since the release last had a ready
    -- container. Containers of an HTTP release follow traffic rather than
    -- tasks, so this stops planning from starting containers that would fail
    -- the same way.
    load_error text,
    unique (workload_id, version)
);

create index releases_workload_digest on releases (workload_id, spec_digest);

alter table workloads
    add foreign key (active_release_id) references releases (id);

-- Preview releases take negative versions from this sequence, so they never
-- collide with deployed versions and never become active.
create sequence preview_versions;

-- A preview keeps one container of its release while its lease lives: the
-- CLI renews it by following the preview's output.
create table previews (
    release_id uuid primary key references releases (id) on delete cascade,
    workspace_id uuid not null references workspaces (id) on delete cascade,
    user_id uuid not null references users (id) on delete cascade,
    kind text not null check (kind in ('function', 'endpoint', 'asgi', 'realtime')),
    lease_expires_at timestamptz not null,
    deadline_at timestamptz,
    stopped_at timestamptz,
    created_at timestamptz not null default now()
);

create index previews_live on previews (lease_expires_at) where stopped_at is null;

-- Images

-- An image is global and content-addressed: digest covers the rendered
-- Dockerfile with every FROM pinned by digest, the architecture, the build
-- context digest and the GPU hint. reference is the pullable OCI reference by
-- digest; it is null until a build publishes it.
--
-- build_secrets maps each workspace secret the build reads to the version
-- the definition resolved; with the workspace it is part of the image
-- identity, so an image built with one workspace's secrets is never
-- another's. The values are read when the build starts and reach only the
-- builder's secret mount. build_gpu is the GPU model the build container
-- runs on.
create table images (
    digest bytea primary key check (length(digest) = 32),
    id text not null unique check (id ~ '^img_[0-9a-f]{24}$'),
    dockerfile text not null,
    python_version text not null,
    architecture text not null check (architecture in ('amd64', 'arm64')),
    reference text,
    created_at timestamptz not null default now(),
    ready_at timestamptz,
    build_secrets jsonb not null default '{}',
    build_gpu text not null default '',
    check ((reference is null) = (ready_at is null))
);

-- A workspace may deploy an image only after it resolved the image's
-- definition itself, which proves it holds every input. reference is set when
-- the workspace rebuilt a published image; it replaces the global reference
-- for this workspace only.
create table workspace_images (
    workspace_id uuid not null references workspaces (id) on delete cascade,
    image_digest bytea not null references images (digest) on delete cascade,
    reference text,
    ready_at timestamptz,
    created_at timestamptz not null default now(),
    check ((reference is null) = (ready_at is null)),
    primary key (workspace_id, image_digest)
);

-- One build per image may be building; a concurrent request joins it.
-- workspace_id started the build: its containers count against that
-- workspace, and the context archive the build reads is stored there.
-- registry_auth holds the logins for private base images and is cleared when
-- the build ends. A forced build rebuilds a published image for workspace_id
-- alone. log_bytes and log_lines count the current attempt's stored output.
create table image_builds (
    id uuid primary key default uuidv7(),
    image_digest bytea not null references images (digest) on delete cascade,
    state text not null check (state in ('building', 'succeeded', 'failed')),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    forced boolean not null default false,
    context_sha256 bytea check (length(context_sha256) = 32),
    registry_auth jsonb,
    failure text,
    log_bytes bigint not null default 0,
    log_lines integer not null default 0,
    created_at timestamptz not null default now(),
    deadline_at timestamptz not null,
    finished_at timestamptz,
    check ((state = 'building') = (finished_at is null))
);

create unique index image_builds_building on image_builds (image_digest) where state = 'building' and not forced;
create unique index image_builds_building_forced on image_builds (image_digest, workspace_id) where state = 'building' and forced;
create index image_builds_image on image_builds (image_digest, created_at);

create table image_build_logs (
    id bigint generated always as identity primary key,
    build_id uuid not null references image_builds (id) on delete cascade,
    attempt integer not null,
    data text not null,
    logged_at timestamptz not null
);

create index image_build_logs_build on image_build_logs (build_id, id);

-- The GPU model a build container needs, in the form of a release's gpu
-- list, or null for a CPU build or a workload container. Placement and
-- capacity read it where they read a release's.
create function build_gpus(build uuid) returns jsonb
language sql stable
return case when build is null then null else (
    select jsonb_build_array(i.build_gpu)
    from image_builds b
    join images i on i.digest = b.image_digest
    where b.id = build and i.build_gpu <> ''
) end;

-- Execution: tasks, attempts, containers, logs, demand, leases, pods and
-- snapshots.

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
    finished_at timestamptz,
    -- Upstream tasks that have not succeeded yet. Claims and planning take
    -- only queued tasks without any.
    unmet_dependencies integer not null default 0 check (unmet_dependencies >= 0),
    -- Lineage: a task spawned from inside a running task records it as
    -- parent, and every task of one call graph names the same root.
    parent_task_id uuid references tasks (id) on delete set null,
    root_task_id uuid references tasks (id) on delete set null,
    -- The cron occurrence that admitted the task.
    scheduled_for timestamptz,
    -- The W3C traceparent of the request that submitted the task, when it
    -- was traced, so the attempt's spans on the host join that trace.
    traceparent text
);

create index tasks_queued on tasks (release_id, available_at, id) where status = 'queued';
create index tasks_running on tasks (release_id) where status = 'running';
create index tasks_workload_queued on tasks (workload_id) where status = 'queued';
create index tasks_workspace_recent on tasks (workspace_id, id desc);
create index tasks_workload_recent on tasks (workload_id, id desc);
create index tasks_root on tasks (root_task_id) where root_task_id is not null;
create index tasks_parent on tasks (parent_task_id) where parent_task_id is not null;
-- One task per occurrence of a schedule.
create unique index tasks_schedule_occurrence on tasks (workload_id, scheduled_for)
    where scheduled_for is not null;

create table task_inputs (
    task_id uuid primary key references tasks (id) on delete cascade,
    encoding text not null check (encoding in ('json', 'cloudpickle')),
    data bytea not null
);

-- display is how a cloudpickle result shows without loading it: the
-- ResultDisplay the runner made of the value where it ran, checked by the
-- host session. The dashboard renders it; nothing on the server unpickles the
-- result.
create table task_results (
    task_id uuid primary key references tasks (id) on delete cascade,
    encoding text not null check (encoding in ('json', 'cloudpickle')),
    data bytea not null,
    display jsonb
);

create table task_dependencies (
    task_id uuid not null references tasks (id) on delete cascade,
    depends_on uuid not null references tasks (id) on delete cascade,
    primary key (task_id, depends_on)
);

create index task_dependencies_upstream on task_dependencies (depends_on);

-- A container row is placed at most once; host_id never changes after
-- assignment, so the container id fences stale hosts. A container runs
-- either a release's workload or one image build attempt.
create table containers (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    release_id uuid references releases (id) on delete cascade,
    state text not null check (state in ('pending', 'starting', 'ready', 'draining', 'stopped')),
    host_id uuid references hosts (id),
    slots integer not null check (slots >= 1),
    cpu_millis bigint not null,
    memory_bytes bigint not null,
    -- exited: a pod's command ended on its own, with exit_code.
    stop_reason text check (stop_reason in (
        'stopped', 'load_error', 'start_failed', 'crashed', 'out_of_memory', 'host_lost', 'exited'
    )),
    exit_message text,
    created_at timestamptz not null default now(),
    assigned_at timestamptz,
    ready_at timestamptz,
    drain_started_at timestamptz,
    stopped_at timestamptz,
    image_build_id uuid references image_builds (id) on delete cascade,
    -- Why a pending container is still waiting for compute: a host is being
    -- provisioned for it, or the fleet limit holds it back. The capacity
    -- controller writes it; pending reasons read it.
    capacity_wait text check (capacity_wait in ('provisioning', 'limit')),
    -- The host bought for the container. When it is ready and the container
    -- still fits nowhere, its offer cools down instead of being bought again.
    capacity_host_id uuid references hosts (id) on delete set null,
    -- What a container prices at. Planning records the GPUs and placement
    -- class its release asks for; placement records the GPU model and whose
    -- machine it got. The defaults are a CPU container placed automatically
    -- on the platform fleet.
    gpu_count integer not null default 0 check (gpu_count >= 0),
    gpu_type text not null default '',
    rate_class text not null default 'auto'
        check (rate_class in ('auto', 'pinned', 'non_preemptible', 'pinned_non_preemptible')),
    billing_owner text not null default 'platform_fleet'
        check (billing_owner in ('platform_fleet', 'connected_cloud', 'self_hosted')),
    -- purpose: serve containers are the ones planning counts for a release;
    -- an instance (Pod.create, Sandbox.create) or a shell container was
    -- started on request and planning never counts, drains or replaces it.
    --
    -- active_until: the container is not idle before this time; null never
    -- idles. Activity pushes it to now + keep_warm_seconds. A container is
    -- idle once it passed and no container_leases row is unexpired: an idle
    -- instance stops, and planning stops an idle pod container above its
    -- count.
    purpose text not null default 'serve' check (purpose in ('serve', 'instance', 'shell')),
    keep_warm_seconds integer check (keep_warm_seconds >= 0),
    active_until timestamptz,
    -- An instance's command in place of its release's.
    command text[],
    -- The memory snapshot the container starts from.
    snapshot_id uuid,
    block_network boolean not null default false,
    allow_list text[] not null default '{}',
    -- Counts policy changes after the start, which hosts apply in order.
    network_version integer not null default 0,
    -- The newest policy version the host reported applying, and why it
    -- could not when it failed.
    network_applied_version integer not null default 0,
    network_error text,
    -- Ports exposed after the start; a release's own ports are always
    -- exposed.
    exposed_ports integer[] not null default '{}',
    exit_code integer,
    -- The function container that started an instance; only it may drive
    -- the instance through the container API.
    created_by_container uuid references containers (id) on delete set null,
    check ((state = 'pending') = (host_id is null) or state = 'stopped'),
    constraint containers_owner check ((release_id is null) <> (image_build_id is null))
);

create index containers_live_release on containers (release_id) where state <> 'stopped';
create index containers_live_host on containers (host_id) where state <> 'stopped';
create index containers_pending on containers (created_at) where state = 'pending';
-- Deletion waits for a workspace's live containers to stop.
create index containers_live_workspace on containers (workspace_id) where state <> 'stopped';
create index containers_workspace_recent on containers (workspace_id, id desc);
create index containers_workspace_live on containers (workspace_id, id desc) where state <> 'stopped';
create index containers_image_build on containers (image_build_id, created_at) where image_build_id is not null;
-- Metering finds containers that stopped since its last pass.
create index containers_stopped_metering on containers (stopped_at) where ready_at is not null;
-- Cold starts per workload and container activity over a range.
create index containers_release_recent on containers (release_id, id) where release_id is not null;
create index containers_stopped_recent on containers (workspace_id, stopped_at) where state = 'stopped';
create index containers_live_instances on containers (active_until)
    where purpose <> 'serve' and state <> 'stopped';
-- Sandboxes are instance containers. Listing, searching and counting them
-- reads this index rather than every container the workspace ever ran.
create index containers_instances_recent on containers (workspace_id, id desc) where purpose = 'instance';

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
-- Host commands resend cancels of attempts that ended without their slot
-- knowing. Reading only those attempts keeps each host report independent of
-- how many attempts a live container finished recently.
create index attempts_ended_unseen on attempts (container_id, finished_at)
    where state in ('cancelled', 'timed_out', 'lost');

alter table tasks
    add foreign key (current_attempt_id) references attempts (id);

-- Logs by task, workload and container each read their own index instead of
-- every task's lines. Concurrent writers commit identity values out of
-- order, so readers order lines by (writer, id): every transaction below the
-- oldest running one has finished, and a line that commits later sorts after
-- everything a reader already returned.
create table task_logs (
    id bigint generated always as identity primary key,
    task_id uuid not null references tasks (id) on delete cascade,
    attempt integer not null,
    stream text not null check (stream in ('stdout', 'stderr', 'system')),
    data text not null,
    logged_at timestamptz not null,
    workload_id uuid not null,
    container_id uuid not null,
    writer xid8 not null default pg_current_xact_id()
);

create index task_logs_task on task_logs (task_id, writer, id);
create index task_logs_workload on task_logs (workload_id, writer, id);
create index task_logs_container on task_logs (container_id, writer, id);

-- Container output outside task attempts: imports, endpoint requests and
-- previews.
create table container_logs (
    id bigint generated always as identity primary key,
    container_id uuid not null references containers (id) on delete cascade,
    stream text not null check (stream in ('stdout', 'stderr', 'system')),
    data text not null,
    logged_at timestamptz not null,
    -- The request, by its X-Request-Id, an HTTP worker wrote the line for.
    request_id uuid
);

create index container_logs_container on container_logs (container_id, id);
create index container_logs_request on container_logs (request_id, id) where request_id is not null;
create index container_logs_logged on container_logs (logged_at);

-- One edge's demand on one release: requests in flight, requests waiting for
-- a container, and the largest demand within the release's keep-warm window.
-- Edges renew a row while the release has demand; planning ignores rows past
-- expires_at, so a stopped edge's demand lapses on its own.
create table endpoint_loads (
    release_id uuid not null references releases (id) on delete cascade,
    edge_id uuid not null,
    in_flight integer not null check (in_flight >= 0),
    waiting integer not null check (waiting >= 0),
    window_peak integer not null check (window_peak >= 0),
    updated_at timestamptz not null default now(),
    expires_at timestamptz not null,
    primary key (release_id, edge_id)
);

create index endpoint_loads_expires on endpoint_loads (expires_at);

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

-- Secrets. Each value is sealed with its own data key, and the data key is
-- wrapped by a master key held outside PostgreSQL. key_id names that master
-- key, so keys can rotate without rewriting every row at once.
create table secrets (
    workspace_id uuid not null references workspaces (id) on delete cascade,
    name text not null check (name ~ '^[A-Za-z_][A-Za-z0-9_]{0,239}$'),
    key_id text not null,
    wrapped_key bytea not null,
    nonce bytea not null check (length(nonce) = 12),
    ciphertext bytea not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (workspace_id, name)
);

-- Schedules. The expression is a copy of the active release's cron, written
-- by the deploy that activated it; the row holds the firing state.
create table schedules (
    workload_id uuid primary key references workloads (id) on delete cascade,
    expression text not null,
    next_fire_at timestamptz not null,
    -- The occurrence most recently handled, and the task it admitted or why
    -- it admitted none.
    last_fired_at timestamptz,
    last_task_id uuid references tasks (id) on delete set null,
    last_error text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index schedules_due on schedules (next_fire_at);

-- Edge: the hostnames HTTP workloads answer on, custom domains, requests and
-- the edges that relay them.

-- The hostnames a deployed workload answers on. Control claims them in the
-- deploy transaction, so no two workloads share a subdomain or a custom
-- hostname.
create table http_routes (
    workload_id uuid primary key references workloads (id) on delete cascade,
    subdomain text not null unique,
    hostname text unique
);

-- Account-level registrations of customer hostnames. The provider is the
-- authority on whether a hostname serves; the edge routes only ready ones.
create table custom_domains (
    id uuid primary key default uuidv7(),
    user_id uuid not null references users (id) on delete cascade,
    hostname text not null unique,
    phase text not null check (phase in ('awaiting_verification', 'validating', 'ready', 'action_required')),
    provider_hostname_id text,
    required_records jsonb not null default '[]',
    error_code text check (error_code in (
        'verification_timed_out', 'certificate_failed', 'hostname_rejected', 'upstream_unavailable'
    )),
    error_message text,
    verified_at timestamptz,
    last_checked_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index custom_domains_user on custom_domains (user_id, hostname);
create index custom_domains_unsettled on custom_domains (last_checked_at)
    where phase in ('awaiting_verification', 'validating');

-- One row per endpoint or ASGI request, which the edge writes in batches
-- after each ends. The id is the X-Request-Id the caller and the workload
-- saw. Rows older than the retention are deleted.
create table http_requests (
    id uuid primary key,
    workspace_id uuid not null references workspaces (id) on delete cascade,
    workload_id uuid not null references workloads (id) on delete cascade,
    release_id uuid not null references releases (id) on delete cascade,
    container_id uuid,
    method text not null,
    path text not null,
    status integer not null,
    started_at timestamptz not null,
    duration_ms bigint not null,
    request_bytes bigint not null,
    response_bytes bigint not null
);

create index http_requests_workload on http_requests (workload_id, id desc);
create index http_requests_started on http_requests (started_at);
create index http_requests_workspace on http_requests (workspace_id, id desc);

-- Each running server's edge, renewed while it runs. An edge relays a
-- request for a host whose data connection another edge holds to that
-- edge's relay address, authenticated by the token whose digest is here.
create table edges (
    id uuid primary key,
    relay_address text not null,
    token_sha256 bytea not null,
    expires_at timestamptz not null
);

-- The edge holding each host's data connection; the edge writes it when the
-- agent's Listen call opens and removes it when it ends.
create table host_data_links (
    host_id uuid primary key references hosts (id) on delete cascade,
    edge_id uuid not null references edges (id) on delete cascade,
    updated_at timestamptz not null default now()
);

-- Wake-ups for the edge's route table and container sets. They are only
-- signals: the edge re-reads durable state on every one.
create function lc_notify_route() returns trigger language plpgsql as $$
begin
    perform pg_notify('lc_route', '');
    return null;
end
$$;

create trigger workloads_route after insert or delete or update of active_release_id, desired_state on workloads
    for each statement execute function lc_notify_route();
create trigger apps_route after delete or update of state on apps
    for each statement execute function lc_notify_route();
create trigger http_routes_route after insert or update or delete on http_routes
    for each statement execute function lc_notify_route();
create trigger custom_domains_route after insert or delete or update of phase on custom_domains
    for each statement execute function lc_notify_route();

create function lc_notify_endpoint() returns trigger language plpgsql as $$
begin
    perform pg_notify('lc_endpoint', r.workload_id::text) from releases r where r.id = new.release_id;
    return null;
end
$$;

create trigger containers_endpoint after update of state on containers
    for each row when (old.state is distinct from new.state)
    execute function lc_notify_endpoint();

-- Callbacks: an outbox row per retry or terminal transition of a task whose
-- release has a callback_url, written in the transition's transaction, and
-- one per endpoint or ASGI request of such a release once it ends. The edge
-- writes a request's row in the transaction that records the request.
-- release_id is the request's release: at most a bounded number of its
-- callbacks wait at once, and the edge records the rest as failed with the
-- reason.
create table task_callbacks (
    id bigint generated always as identity primary key,
    task_id uuid references tasks (id) on delete cascade,
    workspace_id uuid not null references workspaces (id) on delete cascade,
    url text not null,
    event text not null check (event in ('retry', 'succeeded', 'failed', 'cancelled')),
    attempt integer not null,
    max_attempts integer not null,
    -- The failure that caused a retry event; terminal events read the task.
    failure jsonb,
    state text not null default 'pending' check (state in ('pending', 'delivered', 'failed')),
    deliveries integer not null default 0,
    next_attempt_at timestamptz not null default now(),
    last_error text,
    created_at timestamptz not null default now(),
    finished_at timestamptz,
    request_id uuid references http_requests (id) on delete cascade,
    release_id uuid references releases (id) on delete cascade,
    unique (task_id, event, attempt),
    constraint task_callbacks_one_subject check (num_nonnulls(task_id, request_id) = 1),
    constraint task_callbacks_request unique (request_id),
    constraint task_callbacks_request_release check ((request_id is null) = (release_id is null))
);

create index task_callbacks_due on task_callbacks (next_attempt_at) where state = 'pending';
create index task_callbacks_finished on task_callbacks (finished_at) where state <> 'pending';
-- Pending request callbacks per release, which the cap counts.
create index task_callbacks_pending_release on task_callbacks (release_id) where state = 'pending' and release_id is not null;
-- Due callbacks per workspace, which claims take in turn.
create index task_callbacks_due_workspace on task_callbacks (workspace_id, next_attempt_at, id) where state = 'pending';

-- Storage: sources, volumes, disks, artifacts, queues and maps. PostgreSQL
-- holds metadata and authority; object stores hold the bytes.

create table source_objects (
    workspace_id uuid not null references workspaces (id) on delete cascade,
    sha256 bytea not null check (length(sha256) = 32),
    size_bytes bigint not null check (size_bytes > 0),
    created_at timestamptz not null default now(),
    primary key (workspace_id, sha256)
);

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

-- Billing: accounts and plans, credit, usage metering, the ledger, balances
-- and Stripe operations. A workspace bills its owner's account.

-- An account is created on first use with its trial credit. Free accounts
-- have no Stripe subscription, and an account has no Stripe customer until
-- it saves a card, buys credit or changes plan. A subscription's state is a
-- copy Stripe deliveries refresh.
create table billing_accounts (
    user_id uuid primary key references users (id) on delete cascade,
    terms_version text not null default 'free-v2' check (terms_version in ('free-v2', 'team-v3', 'business-v2')),
    status text not null default 'active' check (status in ('active', 'past_due')),
    stripe_customer_id text unique,
    stripe_subscription_id text unique,
    period_started_at timestamptz,
    period_ended_at timestamptz,
    scheduled_terms_version text check (scheduled_terms_version in ('free-v2', 'team-v3', 'business-v2')),
    scheduled_change_at timestamptz,
    payment_method_attached_at timestamptz,
    complimentary_since timestamptz,
    -- Spending controls. A null limit is no limit.
    monthly_usage_limit_nanos bigint check (monthly_usage_limit_nanos >= 0),
    reload_enabled boolean not null default false,
    reload_threshold_cents integer not null default 1000 check (reload_threshold_cents between 0 and 100000),
    reload_amount_cents integer not null default 2000 check (reload_amount_cents between 500 and 100000),
    -- An automatic payment that was declined or needs authentication pauses
    -- reload until the account resumes it.
    reload_paused_purchase_id uuid,
    reload_pause_reason text check (reload_pause_reason in ('declined', 'action_required')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check ((scheduled_terms_version is null) = (scheduled_change_at is null)),
    check ((reload_paused_purchase_id is null) = (reload_pause_reason is null))
);

create index billing_accounts_reload on billing_accounts (user_id) where reload_enabled and reload_paused_purchase_id is null;

-- Credit an account holds: the one-time trial, subscription credit for one
-- billing period and purchased credit, which never expires. source names
-- what granted it, so a grant is written once. spent_nanos is what the
-- rollup has covered with it; reversed_nanos is refunded or disputed credit.
create table credit_lots (
    id uuid primary key default uuidv7(),
    user_id uuid not null references billing_accounts (user_id) on delete cascade,
    kind text not null check (kind in ('trial', 'subscription', 'purchased')),
    source text not null,
    amount_nanos bigint not null check (amount_nanos > 0),
    reversed_nanos bigint not null default 0 check (reversed_nanos between 0 and amount_nanos),
    spent_nanos bigint not null default 0,
    effective_at timestamptz not null,
    expires_at timestamptz,
    created_at timestamptz not null default now(),
    unique (user_id, source),
    check (kind <> 'purchased' or expires_at is null),
    check (expires_at is null or expires_at > effective_at)
);

create index credit_lots_unspent on credit_lots (user_id, expires_at) where spent_nanos <> amount_nanos - reversed_nanos;

-- One metered interval of one source: the usage fact and its price in one
-- immutable row. A source's entries are contiguous and keyed by their start;
-- they end on UTC quarter-hours, published rate changes and the source's
-- end. A container prices its shape; a volume, an app's artifacts or a disk
-- price the bytes they stored, and a disk also its declared size while a
-- container held it. Workspace, app and workload ids carry no foreign keys so
-- the charge outlives what incurred it.
create table ledger_entries (
    id bigint generated always as identity primary key,
    source_kind text not null check (source_kind in ('container', 'volume', 'artifacts', 'disk', 'egress')),
    source_id uuid not null,
    started_at timestamptz not null,
    ended_at timestamptz not null,
    user_id uuid not null references billing_accounts (user_id) on delete cascade,
    workspace_id uuid not null,
    app_id uuid,
    workload_id uuid,
    category text check (category in ('image-build', 'volume', 'artifacts', 'disk')),
    billing_owner text not null check (billing_owner in ('platform_fleet', 'connected_cloud', 'self_hosted')),
    rate_class text not null check (rate_class in ('auto', 'pinned', 'non_preemptible', 'pinned_non_preemptible')),
    gpu_type text,
    gpu_count integer not null check (gpu_count >= 0),
    cpu_millis bigint not null check (cpu_millis >= 0),
    memory_bytes bigint not null check (memory_bytes >= 0),
    pricing_version text not null,
    container_nanos bigint not null check (container_nanos >= 0),
    cpu_nanos bigint not null check (cpu_nanos >= 0),
    memory_nanos bigint not null check (memory_nanos >= 0),
    gpu_nanos bigint not null check (gpu_nanos >= 0),
    stored_bytes bigint not null default 0 check (stored_bytes >= 0),
    attached_bytes bigint not null default 0 check (attached_bytes >= 0),
    storage_nanos bigint not null default 0 check (storage_nanos >= 0),
    attached_nanos bigint not null default 0 check (attached_nanos >= 0),
    egress_bytes bigint not null default 0 check (egress_bytes >= 0),
    egress_nanos bigint not null default 0 check (egress_nanos >= 0),
    cost_nanos bigint generated always as (
        container_nanos + cpu_nanos + memory_nanos + gpu_nanos + storage_nanos + attached_nanos + egress_nanos) stored,
    unique (source_kind, source_id, started_at),
    check (ended_at > started_at)
);

create index ledger_entries_payer on ledger_entries (user_id, started_at);

-- How far each source is metered. complete marks a stopped container whose
-- last entry is written; such rows are removed once the metering look-back
-- passes them, and storage rows once their source has gone for a day.
create table usage_cursors (
    source_kind text not null,
    source_id uuid not null,
    billed_through timestamptz not null,
    complete boolean not null default false,
    updated_at timestamptz not null default now(),
    primary key (source_kind, source_id)
);

create index usage_cursors_complete on usage_cursors (updated_at) where complete;

-- Internet egress the edge counted, per workload and UTC quarter-hour.
-- Metering prices a quarter once it has closed and removes its rows in the
-- same transaction. Absent app and workload are the nil uuid.
create table egress_quarters (
    workspace_id uuid not null,
    app_id uuid not null,
    workload_id uuid not null,
    quarter timestamptz not null,
    bytes bigint not null check (bytes >= 0),
    primary key (workspace_id, app_id, workload_id, quarter)
);

create index egress_quarters_quarter on egress_quarters (quarter);

-- The metering pass's look-back over stopped containers starts here.
create table metering_state (
    singleton boolean primary key default true check (singleton),
    stopped_since timestamptz not null
);

-- An account's cost per UTC hour, added to as entries are written, and how
-- much of it credit covered. uncovered = cost - credited - waived is debt.
create table billing_hours (
    user_id uuid not null references billing_accounts (user_id) on delete cascade,
    hour timestamptz not null,
    cost_nanos bigint not null default 0,
    credited_nanos bigint not null default 0,
    subscription_nanos bigint not null default 0,
    waived_nanos bigint not null default 0,
    primary key (user_id, hour),
    check (credited_nanos + waived_nanos <= cost_nanos and subscription_nanos <= credited_nanos)
);

create index billing_hours_uncovered on billing_hours (user_id, hour) where credited_nanos + waived_nanos < cost_nanos;

-- An account's balance as of its last rollup. due asks for a rollup;
-- recheck_at is the next instant the balance changes without new usage: a
-- lot with credit left expiring, or the next month. accrued_nanos is the
-- cost of the open intervals of the account's live containers, written by
-- each metering pass with their count. Admission and enforcement read
-- balance_nanos - accrued_nanos.
create table billing_balances (
    user_id uuid primary key references billing_accounts (user_id) on delete cascade,
    balance_nanos bigint not null default 0,
    accrued_nanos bigint not null default 0,
    live_containers integer not null default 0,
    month_started_at timestamptz not null,
    month_spent_nanos bigint not null default 0,
    due boolean not null default true,
    recheck_at timestamptz not null,
    rolled_at timestamptz
);

create index billing_balances_due on billing_balances (user_id) where due;
create index billing_balances_recheck on billing_balances (recheck_at);
create index billing_balances_live on billing_balances (user_id) where live_containers > 0;
create index billing_balances_unfunded on billing_balances (user_id) where balance_nanos - accrued_nanos <= 0;

-- An account without credit that stores data: its data is kept, free of
-- charges, for 30 days from started_at and then deleted. Credit that
-- restores a positive balance ends the period. message_id is the warning
-- email, withdrawn if still unsent when the period ends.
create table unfunded_periods (
    user_id uuid primary key references billing_accounts (user_id) on delete cascade,
    started_at timestamptz not null default now(),
    message_id uuid references email_outbox (id) on delete set null
);

create index unfunded_periods_started on unfunded_periods (started_at);

-- A plan change, inserted before Stripe is called so its id keys the call.
-- One per account is open at a time.
create table plan_changes (
    id uuid primary key default uuidv7(),
    user_id uuid not null references billing_accounts (user_id) on delete cascade,
    from_terms text not null,
    to_terms text not null,
    state text not null default 'open' check (state in ('open', 'applied', 'failed')),
    attempts integer not null default 0,
    next_attempt_at timestamptz not null default now(),
    lease_until timestamptz,
    error text not null default '',
    created_at timestamptz not null default now(),
    finished_at timestamptz
);

create unique index plan_changes_open on plan_changes (user_id) where state = 'open';
create index plan_changes_due on plan_changes (next_attempt_at) where state = 'open';

-- A credit purchase through Checkout (manual) or an off-session charge of
-- the saved card (automatic), inserted before Stripe is called.
create table credit_purchases (
    id uuid primary key default uuidv7(),
    user_id uuid not null references billing_accounts (user_id) on delete cascade,
    kind text not null check (kind in ('manual', 'automatic')),
    request_key uuid,
    amount_nanos bigint not null check (amount_nanos > 0),
    status text not null default 'pending'
        check (status in ('pending', 'action_required', 'succeeded', 'declined', 'cancelled')),
    success_url text not null default '',
    cancel_url text not null default '',
    checkout_session_id text unique,
    checkout_url text,
    checkout_expires_at timestamptz,
    payment_intent_id text unique,
    lot_id uuid references credit_lots (id) on delete set null,
    reversed_nanos bigint not null default 0 check (reversed_nanos >= 0),
    attempts integer not null default 0,
    next_attempt_at timestamptz not null default now(),
    last_error text not null default '',
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    funded_at timestamptz,
    check ((kind = 'manual') = (request_key is not null))
);

create unique index credit_purchases_request on credit_purchases (user_id, request_key) where request_key is not null;
-- One automatic payment at a time.
create unique index credit_purchases_automatic_open on credit_purchases (user_id)
    where kind = 'automatic' and status = 'pending';
create index credit_purchases_due on credit_purchases (next_attempt_at) where status = 'pending';
create index credit_purchases_user on credit_purchases (user_id, created_at);

-- Verified Stripe deliveries. The scheduler processes each by fetching the
-- object it names again; processed rows are removed after a retention
-- period.
create table stripe_events (
    id text primary key,
    type text not null,
    object_id text not null,
    -- The customer the event names, kept because a detached payment
    -- method no longer names it when fetched again.
    customer_id text,
    received_at timestamptz not null default now(),
    attempts integer not null default 0,
    next_attempt_at timestamptz not null default now(),
    lease_until timestamptz,
    processed_at timestamptz,
    last_error text not null default ''
);

create index stripe_events_due on stripe_events (next_attempt_at) where processed_at is null;
create index stripe_events_processed on stripe_events (processed_at) where processed_at is not null;

-- Observability: container metrics, startup stages and the change stream.

-- Container metrics. Agents sample each running container every 5 s and
-- send one batch per host over the session. Samples are kept an hour; a
-- scheduler pass folds every finished minute into container_metric_minutes,
-- kept seven days. Counters are deltas over interval_ms; gauges are the
-- value when sampled.
create table container_metric_samples (
    container_id uuid not null references containers (id) on delete cascade,
    sampled_at timestamptz not null,
    interval_ms integer not null check (interval_ms > 0),
    cpu_usage_usec bigint not null,
    memory_rss_bytes bigint not null,
    memory_swap_bytes bigint not null,
    network_rx_bytes bigint not null,
    network_tx_bytes bigint not null,
    disk_read_bytes bigint not null,
    disk_write_bytes bigint not null,
    -- Averaged over the container's GPUs; null without one.
    gpu_utilization_pct real,
    gpu_memory_used_bytes bigint,
    gpu_memory_total_bytes bigint,
    gpu_type text,
    primary key (container_id, sampled_at)
);

create index container_metric_samples_age on container_metric_samples (sampled_at);

-- One row per container and minute: counters summed, memory and GPU memory
-- at their peak, GPU utilization averaged over the samples.
create table container_metric_minutes (
    container_id uuid not null references containers (id) on delete cascade,
    minute timestamptz not null,
    samples integer not null,
    interval_ms bigint not null,
    cpu_usage_usec bigint not null,
    memory_rss_bytes bigint not null,
    memory_swap_bytes bigint not null,
    network_rx_bytes bigint not null,
    network_tx_bytes bigint not null,
    disk_read_bytes bigint not null,
    disk_write_bytes bigint not null,
    gpu_utilization_pct real,
    gpu_memory_used_bytes bigint,
    gpu_memory_total_bytes bigint,
    gpu_type text,
    primary key (container_id, minute)
);

create index container_metric_minutes_age on container_metric_minutes (minute);

-- The rollup's watermark: every sample before rolled_through is folded.
create table container_metric_rollup (
    only_row boolean primary key default true check (only_row),
    rolled_through timestamptz not null
);

-- How long each stage of a container's start took on its host. The host
-- restates the stages in its reports; the first report of a stage wins.
-- disk: the agent leased and restored the container's disks.
create table container_startup_stages (
    container_id uuid not null references containers (id) on delete cascade,
    stage text not null check (stage in ('image', 'source', 'create', 'runtime', 'disk')),
    started_at timestamptz not null,
    finished_at timestamptz not null,
    -- For image: the image was already on the host.
    cached boolean not null default false,
    primary key (container_id, stage)
);

-- Change stream. Statement-level triggers send one notification per
-- statement and workspace on lc_changes, delivered at commit:
--
--   {"seq": 41, "workspace_id": "…", "occurred_at": "…",
--    "changes": [{"topic": "tasks", "change": "updated", "resource_id": "…",
--                 "app_id": "…", "deployment_id": "…", "task_id": "…",
--                 "root_task_id": "…", "status": "running"}]}
--
-- seq comes from a sequence: unique, but not in commit order; servers keep
-- events in the order notifications arrive, which is commit order. A
-- statement whose changes exceed one notification sends them grouped by
-- topic, app and deployment with a count and no resource_id, then by topic
-- alone. Every published resource's trigger calls publish_changes.
create sequence change_seq;

create function publish_changes(workspace uuid, items jsonb) returns void
language plpgsql as $$
declare
    -- pg_notify refuses payloads of 8000 bytes or more.
    budget constant int := 7000;
begin
    if items is null or jsonb_array_length(items) = 0 then
        return;
    end if;
    if octet_length(items::text) > budget then
        select jsonb_agg(g) into items from (
            select jsonb_strip_nulls(jsonb_build_object(
                'topic', i->>'topic', 'change', 'updated', 'app_id', i->>'app_id',
                'deployment_id', i->>'deployment_id', 'count', count(*))) as g
            from jsonb_array_elements(items) i
            group by i->>'topic', i->>'app_id', i->>'deployment_id'
        ) grouped;
    end if;
    if octet_length(items::text) > budget then
        select jsonb_agg(g) into items from (
            select jsonb_build_object('topic', i->>'topic', 'change', 'updated',
                                      'count', sum(coalesce((i->>'count')::int, 1))) as g
            from jsonb_array_elements(items) i
            group by i->>'topic'
        ) grouped;
    end if;
    perform pg_notify('lc_changes', jsonb_build_object(
        'seq', nextval('change_seq'), 'workspace_id', workspace, 'occurred_at', now(), 'changes', items)::text);
end $$;

-- Each trigger function reads its statement's transition tables, so a
-- statement that changes many rows sends one notification per workspace.
-- A batch submit that cannot fit one notification is grouped before any
-- item is built.
create function observe_created_tasks() returns trigger
language plpgsql as $$
begin
    if (select count(*) from new_rows) > 24 then
        perform publish_changes(c.workspace_id, c.items) from (
            select g.workspace_id, jsonb_agg(jsonb_build_object(
                'topic', 'tasks', 'change', 'created', 'app_id', w.app_id, 'deployment_id', g.workload_id,
                'count', g.tasks)) as items
            from (select workspace_id, workload_id, count(*) as tasks from new_rows group by 1, 2) g
            join workloads w on w.id = g.workload_id
            group by g.workspace_id
        ) c;
        return null;
    end if;
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
            'topic', 'tasks', 'change', 'created', 'resource_id', n.id, 'task_id', n.id,
            'root_task_id', n.root_task_id, 'app_id', w.app_id, 'deployment_id', n.workload_id,
            'status', n.status))) as items
        from new_rows n join workloads w on w.id = n.workload_id
        group by n.workspace_id
    ) c;
    return null;
end $$;

-- A claim moves tasks to running in a transaction that sends no other
-- notification, and a notifying transaction holds PostgreSQL's global
-- notification lock through its commit, WAL flush included. Publishing
-- here would serialize every claim, so running transitions are left out:
-- the server publishes them coalesced, outside the claim (see
-- observability.Started).
create function observe_updated_tasks() returns trigger
language plpgsql as $$
begin
    if (select count(*) from new_rows n join old_rows o on o.id = n.id
        where o.status <> n.status and n.status <> 'running') > 24 then
        perform publish_changes(c.workspace_id, c.items) from (
            select g.workspace_id, jsonb_agg(jsonb_build_object(
                'topic', 'tasks', 'change', 'updated', 'app_id', w.app_id, 'deployment_id', g.workload_id,
                'count', g.tasks)) as items
            from (select n.workspace_id, n.workload_id, count(*) as tasks
                  from new_rows n join old_rows o on o.id = n.id
                  where o.status <> n.status and n.status <> 'running'
                  group by 1, 2) g
            join workloads w on w.id = g.workload_id
            group by g.workspace_id
        ) c;
        return null;
    end if;
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
            'topic', 'tasks', 'change', 'updated', 'resource_id', n.id, 'task_id', n.id,
            'root_task_id', n.root_task_id, 'app_id', w.app_id, 'deployment_id', n.workload_id,
            'status', n.status))) as items
        from new_rows n
        join old_rows o on o.id = n.id
        join workloads w on w.id = n.workload_id
        where o.status <> n.status and n.status <> 'running'
        group by n.workspace_id
    ) c;
    return null;
end $$;

create trigger tasks_created after insert on tasks
    referencing new table as new_rows for each statement execute function observe_created_tasks();
create trigger tasks_updated after update on tasks
    referencing old table as old_rows new table as new_rows for each statement execute function observe_updated_tasks();

-- Image build containers have no release and are not published.
create function observe_created_containers() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'containers', 'change', 'created', 'resource_id', n.id, 'container_id', n.id,
            'app_id', w.app_id, 'deployment_id', w.id, 'status', n.state)) as items
        from new_rows n
        join releases r on r.id = n.release_id
        join workloads w on w.id = r.workload_id
        group by n.workspace_id
    ) c;
    return null;
end $$;

create function observe_updated_containers() returns trigger
language plpgsql as $$
begin
    if (select count(*) from new_rows n join old_rows o on o.id = n.id where o.state <> n.state) > 24 then
        perform publish_changes(c.workspace_id, c.items) from (
            select g.workspace_id, jsonb_agg(jsonb_build_object(
                'topic', 'containers', 'change', 'updated', 'app_id', w.app_id, 'deployment_id', w.id,
                'count', g.containers)) as items
            from (select n.workspace_id, n.release_id, count(*) as containers
                  from new_rows n join old_rows o on o.id = n.id
                  where o.state <> n.state
                  group by 1, 2) g
            join releases r on r.id = g.release_id
            join workloads w on w.id = r.workload_id
            group by g.workspace_id
        ) c;
        return null;
    end if;
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'containers', 'change', 'updated', 'resource_id', n.id, 'container_id', n.id,
            'app_id', w.app_id, 'deployment_id', w.id, 'status', n.state)) as items
        from new_rows n
        join old_rows o on o.id = n.id
        join releases r on r.id = n.release_id
        join workloads w on w.id = r.workload_id
        where o.state <> n.state
        group by n.workspace_id
    ) c;
    return null;
end $$;

create trigger containers_created after insert on containers
    referencing new table as new_rows for each statement execute function observe_created_containers();
create trigger containers_updated after update on containers
    referencing old table as old_rows new table as new_rows for each statement execute function observe_updated_containers();

create function observe_created_apps() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'apps', 'change', 'created', 'resource_id', n.id, 'app_id', n.id, 'status', n.state)) as items
        from new_rows n
        group by n.workspace_id
    ) c;
    return null;
end $$;

create function observe_updated_apps() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'apps', 'change', case when n.state = 'deleted' then 'deleted' else 'updated' end,
            'resource_id', n.id, 'app_id', n.id, 'status', n.state)) as items
        from new_rows n
        join old_rows o on o.id = n.id
        where o.state <> n.state
        group by n.workspace_id
    ) c;
    return null;
end $$;

create trigger apps_created after insert on apps
    referencing new table as new_rows for each statement execute function observe_created_apps();
create trigger apps_updated after update on apps
    referencing old table as old_rows new table as new_rows for each statement execute function observe_updated_apps();

-- A deployment is a workload; a deploy switches its active release.
create function observe_created_workloads() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select a.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'deployments', 'change', 'created', 'resource_id', n.id, 'deployment_id', n.id,
            'app_id', n.app_id, 'status', n.desired_state)) as items
        from new_rows n
        join apps a on a.id = n.app_id
        group by a.workspace_id
    ) c;
    return null;
end $$;

create function observe_updated_workloads() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select a.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'deployments',
            'change', case when n.desired_state = 'deleted' then 'deleted' else 'updated' end,
            'resource_id', n.id, 'deployment_id', n.id, 'app_id', n.app_id, 'status', n.desired_state)) as items
        from new_rows n
        join old_rows o on o.id = n.id
        join apps a on a.id = n.app_id
        where o.desired_state <> n.desired_state or o.active_release_id is distinct from n.active_release_id
        group by a.workspace_id
    ) c;
    return null;
end $$;

create trigger workloads_created after insert on workloads
    referencing new table as new_rows for each statement execute function observe_created_workloads();
create trigger workloads_updated after update on workloads
    referencing old table as old_rows new table as new_rows for each statement execute function observe_updated_workloads();

-- Secrets: names only, never values.
create function observe_written_secrets() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'storage.secrets', 'change', case when tg_op = 'INSERT' then 'created' else 'updated' end,
            'resource_id', n.name)) as items
        from new_rows n group by n.workspace_id
    ) c;
    return null;
end $$;

create function observe_deleted_secrets() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select o.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'storage.secrets', 'change', 'deleted', 'resource_id', o.name)) as items
        from old_rows o group by o.workspace_id
    ) c;
    return null;
end $$;

create trigger secrets_created after insert on secrets
    referencing new table as new_rows for each statement execute function observe_written_secrets();
create trigger secrets_updated after update on secrets
    referencing new table as new_rows for each statement execute function observe_written_secrets();
create trigger secrets_deleted after delete on secrets
    referencing old table as old_rows for each statement execute function observe_deleted_secrets();

-- A volume is created, measured (size_bytes) or deleted; a deleting volume
-- is gone for its users.
create function observe_created_volumes() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'storage.volumes', 'change', 'created', 'resource_id', n.id)) as items
        from new_rows n group by n.workspace_id
    ) c;
    return null;
end $$;

create function observe_updated_volumes() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'storage.volumes',
            'change', case when n.state = 'deleting' then 'deleted' else 'updated' end,
            'resource_id', n.id)) as items
        from new_rows n
        join old_rows o on o.id = n.id
        where o.state <> n.state or o.size_bytes <> n.size_bytes
        group by n.workspace_id
    ) c;
    return null;
end $$;

create function observe_deleted_volumes() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select o.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'storage.volumes', 'change', 'deleted', 'resource_id', o.id)) as items
        from old_rows o group by o.workspace_id
    ) c;
    return null;
end $$;

create trigger volumes_created after insert on volumes
    referencing new table as new_rows for each statement execute function observe_created_volumes();
create trigger volumes_updated after update on volumes
    referencing old table as old_rows new table as new_rows for each statement execute function observe_updated_volumes();
create trigger volumes_deleted after delete on volumes
    referencing old table as old_rows for each statement execute function observe_deleted_volumes();

-- Metering writes ledger entries in batches; each statement tells every
-- workspace it charged once that its usage moved.
create function observe_ledger_entries() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_build_array(jsonb_build_object(
            'topic', 'usage', 'change', 'updated', 'count', count(*))) as items
        from new_rows n group by n.workspace_id
    ) c;
    return null;
end $$;

create trigger ledger_entries_created after insert on ledger_entries
    referencing new table as new_rows for each statement execute function observe_ledger_entries();
