-- Observability: container metrics, startup phases, the change stream's
-- notifications, task trace context and the indexes metric queries read.

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

insert into container_metric_rollup (rolled_through) values (date_trunc('minute', now()));

-- How long each stage of a container's start took on its host. The host
-- restates the stages in its reports; the first report of a stage wins.
create table container_startup_stages (
    container_id uuid not null references containers (id) on delete cascade,
    stage text not null check (stage in ('image', 'source', 'create', 'runtime')),
    started_at timestamptz not null,
    finished_at timestamptz not null,
    -- For image: the image was already on the host.
    cached boolean not null default false,
    primary key (container_id, stage)
);

-- The W3C traceparent of the request that submitted the task, when it was
-- traced, so the attempt's spans on the host join that trace.
alter table tasks add column traceparent text;

-- Cold starts per workload and container activity over a range.
create index containers_release_recent on containers (release_id, id) where release_id is not null;
create index containers_stopped_recent on containers (workspace_id, stopped_at) where state = 'stopped';

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
-- alone. Other owners publish their resources by calling publish_changes
-- from their own triggers.
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
