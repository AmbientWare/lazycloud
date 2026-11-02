-- Webhooks: URLs a workspace registers to receive its events, for every app
-- or for the app of one name, which a redeploy of that name keeps.
create table webhooks (
    id uuid primary key default uuidv7(),
    workspace_id uuid not null references workspaces (id) on delete cascade,
    app_name text check (app_name ~ '^[a-z][a-z0-9_]{0,62}$'),
    url text not null check (char_length(url) between 1 and 2048),
    events text[] not null check (cardinality(events) > 0 and events <@ array[
        'task.succeeded', 'task.failed', 'task.cancelled', 'workload.deployed', 'workload.failed'
    ]),
    description text not null default '' check (char_length(description) <= 200),
    enabled boolean not null default true,
    created_at timestamptz not null default now()
);

create index webhooks_workspace on webhooks (workspace_id, id);

-- An outbox row per event and webhook, written in the transaction of the
-- change the event reports. Every webhook an event reaches shares its
-- event_id, which receivers deduplicate on. data is the event's data as
-- delivered. Delivery follows task_callbacks: claims lease rows in
-- per-workspace turns and deliveries fences a late outcome.
create table webhook_deliveries (
    id bigint generated always as identity primary key,
    webhook_id uuid not null references webhooks (id) on delete cascade,
    workspace_id uuid not null references workspaces (id) on delete cascade,
    event_id uuid not null,
    event text not null,
    data jsonb not null,
    state text not null default 'pending' check (state in ('pending', 'delivered', 'failed')),
    deliveries integer not null default 0,
    next_attempt_at timestamptz not null default now(),
    response_status integer,
    last_error text,
    created_at timestamptz not null default now(),
    finished_at timestamptz
);

create index webhook_deliveries_due on webhook_deliveries (next_attempt_at) where state = 'pending';
create index webhook_deliveries_finished on webhook_deliveries (finished_at) where state <> 'pending';
create index webhook_deliveries_due_workspace on webhook_deliveries (workspace_id, next_attempt_at, id) where state = 'pending';
create index webhook_deliveries_webhook on webhook_deliveries (webhook_id, id desc);

-- lc_webhook_event records one event for every enabled webhook of the
-- workspace that takes it, and wakes delivery when it commits. event_app
-- names the app the event concerns; null reaches only workspace-wide
-- webhooks. It returns how many deliveries it wrote.
create function lc_webhook_event(event_workspace uuid, event_app text, event_type text, event_data jsonb) returns integer
language plpgsql as $$
declare
    shared_id uuid := uuidv7();
    written integer;
begin
    insert into webhook_deliveries (webhook_id, workspace_id, event_id, event, data)
    select h.id, h.workspace_id, shared_id, event_type, event_data
    from webhooks h
    where h.workspace_id = event_workspace and h.enabled and event_type = any(h.events)
      and (h.app_name is null or h.app_name = event_app);
    get diagnostics written = row_count;
    if written > 0 then
        perform pg_notify('lc_callback', '');
    end if;
    return written;
end
$$;
