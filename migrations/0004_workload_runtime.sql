-- Workload runtime: secrets, task lineage, schedules and task callbacks.

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

-- Lineage: a task spawned from inside a running task records it as parent,
-- and every task of one call graph names the same root.
alter table tasks
    add column parent_task_id uuid references tasks (id) on delete set null,
    add column root_task_id uuid references tasks (id) on delete set null,
    -- The cron occurrence that admitted the task.
    add column scheduled_for timestamptz;

create index tasks_root on tasks (root_task_id) where root_task_id is not null;

-- One task per occurrence of a schedule.
create unique index tasks_schedule_occurrence on tasks (workload_id, scheduled_for)
    where scheduled_for is not null;

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

-- Task callbacks: an outbox row per retry or terminal transition of a task
-- whose release has a callback_url, written in the transition's
-- transaction.
create table task_callbacks (
    id bigint generated always as identity primary key,
    task_id uuid not null references tasks (id) on delete cascade,
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
    unique (task_id, event, attempt)
);

create index task_callbacks_due on task_callbacks (next_attempt_at) where state = 'pending';
create index task_callbacks_finished on task_callbacks (finished_at) where state <> 'pending';
