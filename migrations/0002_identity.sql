-- Identity: accounts, sign-in, sessions, device codes, tokens, workspace
-- lifecycle, roles and invitations. Notifications: the email outbox.

-- An account signs in with GitHub once github_user_id is set. Accounts made
-- by the admin command have an email and no GitHub identity until their first
-- sign-in with that verified email.
alter table users
    alter column email drop not null,
    add column display_name text not null default '',
    add column avatar_url text not null default '',
    add column github_user_id bigint unique,
    add column github_login text not null default '',
    add column status text not null default 'active' check (status in ('active', 'disabled')),
    add column updated_at timestamptz not null default now();

-- A deleting workspace refuses every request except its deletion, which the
-- scheduler finishes by removing the row.
alter table workspaces
    add column state text not null default 'active' check (state in ('active', 'deleting')),
    add column deletion_requested_at timestamptz,
    add column updated_at timestamptz not null default now();

create index workspaces_deleting on workspaces (deletion_requested_at) where state = 'deleting';

-- Deletion waits for a workspace's live containers to stop.
create index containers_live_workspace on containers (workspace_id) where state <> 'stopped';

alter table workspace_members drop constraint workspace_members_role_check;
alter table workspace_members
    add constraint workspace_members_role_check check (role in ('owner', 'administrator', 'member'));

-- One owner per workspace; ownership is never offered or changed by a role
-- update.
create unique index workspace_members_one_owner on workspace_members (workspace_id) where role = 'owner';
create index workspace_members_owned on workspace_members (user_id, created_at) where role = 'owner';

-- device marks tokens minted by device-code login; last_used_at is written
-- in batches, so it lags by up to the flush interval.
alter table api_tokens
    add column device boolean not null default false,
    add column last_used_at timestamptz;

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
