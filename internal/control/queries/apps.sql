-- name: ListApps :many
-- Live apps with a deployed workload, by name after the cursor. Reads the
-- live-name index of the workspace. state <> 'stopped' lets the running
-- count read the live-container index of each release.
select a.id, a.name, a.state, a.created_at,
       (select count(*) from workloads w
        where w.app_id = a.id and w.desired_state <> 'deleted' and w.active_release_id is not null)::int as workloads,
       (select count(*) from workloads w
        join releases r on r.workload_id = w.id
        join containers c on c.release_id = r.id
        where w.app_id = a.id and c.state <> 'stopped' and c.state in ('ready', 'draining'))::int as running_containers
from apps a
where a.workspace_id = @workspace_id
  and a.state <> 'deleted'
  and (sqlc.narg(state)::text is null or a.state = sqlc.narg(state)::text)
  and a.name > @after
  and exists (select 1 from workloads w
              where w.app_id = a.id and w.desired_state <> 'deleted' and w.active_release_id is not null)
order by a.name
limit @max_rows;

-- name: AppByName :one
select id from apps where workspace_id = @workspace_id and name = @name and state <> 'deleted';

-- name: LockApp :one
-- An id names a deleted app too; a name only a live one.
select id, name, state, created_at
from apps
where workspace_id = @workspace_id and id = @id
for update;

-- name: AppView :one
select a.id, a.name, a.state, a.created_at,
       (select count(*) from workloads w
        where w.app_id = a.id and w.desired_state <> 'deleted' and w.active_release_id is not null)::int as workloads,
       (select count(*) from workloads w
        join releases r on r.workload_id = w.id
        join containers c on c.release_id = r.id
        where w.app_id = a.id and c.state <> 'stopped' and c.state in ('ready', 'draining'))::int as running_containers
from apps a
where a.workspace_id = @workspace_id and a.id = @id;

-- name: SetAppState :exec
update apps
set state = @state,
    deleted_at = case when @state = 'deleted' then now() else deleted_at end
where id = @id;
