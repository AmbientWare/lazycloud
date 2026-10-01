-- Control: soft deletion of apps and workloads, and releases for
-- working-tree calls.

-- A deleted app or workload keeps its rows for task history and frees its
-- name; execution retires its releases.
alter table apps drop constraint apps_workspace_id_name_key;
alter table apps drop constraint apps_state_check;
alter table apps add constraint apps_state_check check (state in ('active', 'paused', 'deleted'));
alter table apps add column deleted_at timestamptz;
create unique index apps_live_name on apps (workspace_id, name) where state <> 'deleted';

alter table workloads drop constraint workloads_app_id_kind_name_key;
alter table workloads drop constraint workloads_desired_state_check;
alter table workloads add constraint workloads_desired_state_check
    check (desired_state in ('active', 'stopped', 'deleted'));
alter table workloads add column deleted_at timestamptz;
create unique index workloads_live_name on workloads (app_id, kind, name) where desired_state <> 'deleted';

-- A release without a version runs working-tree calls and is never active.
alter table releases alter column version drop not null;
create index releases_workload_digest on releases (workload_id, spec_digest);

-- Execution: task listing, call graphs and dependencies.

alter table tasks
    add column parent_task_id uuid references tasks (id) on delete set null,
    add column root_task_id uuid references tasks (id) on delete set null,
    -- Upstream tasks that have not succeeded yet. Claims and planning take
    -- only queued tasks without any.
    add column unmet_dependencies integer not null default 0 check (unmet_dependencies >= 0);

create index tasks_workspace_recent on tasks (workspace_id, id desc);
create index tasks_workload_recent on tasks (workload_id, id desc);
create index tasks_parent on tasks (parent_task_id) where parent_task_id is not null;
create index tasks_root on tasks (root_task_id) where root_task_id is not null;

create table task_dependencies (
    task_id uuid not null references tasks (id) on delete cascade,
    depends_on uuid not null references tasks (id) on delete cascade,
    primary key (task_id, depends_on)
);

create index task_dependencies_upstream on task_dependencies (depends_on);

-- Logs by workload and by container read their own index instead of every
-- task's lines.
alter table task_logs add column workload_id uuid, add column container_id uuid;
update task_logs l
set workload_id = t.workload_id, container_id = a.container_id
from tasks t, attempts a
where t.id = l.task_id and a.task_id = l.task_id and a.number = l.attempt;
alter table task_logs alter column workload_id set not null, alter column container_id set not null;
create index task_logs_workload on task_logs (workload_id, id);
create index task_logs_container on task_logs (container_id, id);

create index containers_workspace_recent on containers (workspace_id, id desc);
create index containers_workspace_live on containers (workspace_id, id desc) where state <> 'stopped';
