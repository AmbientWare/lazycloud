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

-- A workspace lives on the platform's compute (null) or in one connected
-- account, fixed at creation.
alter table workspaces add column connection_id uuid references cloud_connections (id);

create index workspaces_connection on workspaces (connection_id) where connection_id is not null;

-- Hosts. state stays the session authority (online, offline, lost,
-- retired) that liveness and assignment fence on; phase is the machine
-- lifecycle users see. kind says whose capacity it is: the platform's, a
-- connected account's or an account's joined machine.
alter table hosts
    alter column token_hash drop not null,
    add column kind text not null default 'platform' check (kind in ('platform', 'connection', 'machine')),
    add column provider text not null default 'agent' check (provider in ('agent', 'aws')),
    -- A connection host keeps its row after the connection is removed.
    add column connection_id uuid references cloud_connections (id) on delete set null,
    -- The account that joined a machine.
    add column account_id uuid references users (id) on delete set null,
    add column phase text not null default 'ready' check (phase in (
        'requested', 'provisioning', 'booting', 'joining', 'ready', 'draining', 'stopping', 'stopped',
        'resuming', 'terminating', 'deleted', 'failed'
    )),
    add column phase_message text not null default '',
    add column phase_at timestamptz not null default now(),
    add column failure text check (failure in (
        'agent_download_failed', 'runtime_install_failed', 'network_join_failed', 'provider_identity_failed',
        'agent_enrollment_failed', 'worker_image_pull_failed', 'worker_start_failed',
        'worker_readiness_failed', 'bootstrap_timed_out', 'host_preflight_failed', 'service_lost',
        'machine_record_deleted', 'provider_stopped', 'provider_terminated', 'unknown'
    )),
    add column capacity_state text not null default 'available' check (capacity_state in (
        'available', 'draining', 'preempting', 'cordoned'
    )),
    add column capacity_reason text not null default '',
    add column gpu_type text not null default '',
    add column gpu_count integer not null default 0 check (gpu_count >= 0),
    add column architecture text not null default 'amd64' check (architecture in ('amd64', 'arm64')),
    add column agent_version text not null default '',
    -- Checks a joining host reported, [{name, ok, message, severity, remediation}].
    add column preflight jsonb not null default '[]',
    -- Cloud instances: the offer launched and what the provider reported.
    add column region text not null default '',
    add column availability_zone text not null default '',
    add column availability_zone_id text not null default '',
    add column instance_type text not null default '',
    add column market text check (market in ('spot', 'on_demand')),
    add column hourly_micros bigint,
    add column instance_id text unique,
    add column launch_attempts integer not null default 0,
    -- A launcher holds a requested host until this passes.
    add column launch_lease_until timestamptz,
    add column launched_at timestamptz,
    -- Set while a ready cloud host has no live container.
    add column idle_since timestamptz,
    -- The provider will reclaim the instance at this time.
    add column interruption_at timestamptz,
    add column updated_at timestamptz not null default now();

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
alter table host_join_tokens add column host_id uuid references hosts (id) on delete cascade;

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

-- Why a pending container is still waiting for compute: a host is being
-- provisioned for it, or the fleet limit holds it back. The capacity
-- controller writes it; pending reasons read it.
alter table containers add column capacity_wait text check (capacity_wait in ('provisioning', 'limit'));

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
    created_at timestamptz not null default now(),
    check (sha256_amd64 is not null or sha256_arm64 is not null)
);

create unique index agent_releases_target on agent_releases (target) where target;
