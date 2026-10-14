-- Listings read the ids of a page from an index that holds every column
-- they filter on, so an index-only scan of one range per key answers them
-- whatever the history, and the rows load by id.

-- Whether another task spawned the task; root-only listings leave these out.
alter table tasks add column spawned boolean not null generated always as (parent_task_id is not null) stored;
-- Task listings by status or roots only: of a workspace, of an app's or
-- function's workloads, and of a function version.
create index tasks_workspace_listing on tasks (workspace_id, status, spawned, id desc);
create index tasks_workload_listing on tasks (workload_id, status, spawned, id desc);
create index tasks_release_listing on tasks (release_id, status, spawned, id desc);

-- The workload of a function container, from its release; image build
-- containers have none.
alter table containers add column workload_id uuid references workloads (id) on delete cascade;
update containers c set workload_id = r.workload_id from releases r where r.id = c.release_id;
-- Container listings of an app or workload, all or live.
create index containers_workload_recent on containers (workload_id, id desc);
create index containers_workload_live on containers (workload_id, id desc) where state <> 'stopped';
-- Workspace container listings leave image build containers out.
drop index containers_workspace_recent;
create index containers_workspace_recent on containers (workspace_id, id desc) include (release_id);
drop index containers_workspace_live;
create index containers_workspace_live on containers (workspace_id, id desc) include (release_id) where state <> 'stopped';
