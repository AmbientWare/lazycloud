-- Endpoints: HTTP workloads, the hostnames they answer on, edge demand,
-- custom domains, previews and container output.

alter table workloads drop constraint workloads_kind_check;
alter table workloads add constraint workloads_kind_check check (kind in ('function', 'endpoint', 'asgi'));

-- Preview releases take negative versions from this sequence, so they never
-- collide with deployed versions and never become active.
create sequence preview_versions;

-- The handler's import error since the release last had a ready container.
-- Containers of an HTTP release follow traffic rather than tasks, so this
-- stops planning from starting containers that would fail the same way.
alter table releases add column load_error text;

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

create index http_requests_workload on http_requests (workload_id, id desc);
create index http_requests_started on http_requests (started_at);
create index http_requests_workspace on http_requests (workspace_id, id desc);

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
