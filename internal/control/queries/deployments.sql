-- name: ListDeployments :many
-- Deployed live workloads of live apps by app and name after the cursor.
select w.id, a.name as app_name, w.name, w.kind, w.desired_state, a.state as app_state,
       r.version, r.id as release_id, w.created_at, r.created_at as deployed_at
from apps a
join workloads w on w.app_id = a.id
join releases r on r.id = w.active_release_id
where a.workspace_id = @workspace_id
  and a.state <> 'deleted'
  and w.desired_state <> 'deleted'
  and (sqlc.narg(app)::text is null or a.name = sqlc.narg(app)::text)
  and (sqlc.narg(name)::text is null or w.name = sqlc.narg(name)::text)
  and (a.name, w.name) > (@after_app::text, @after_name::text)
order by a.name, w.name
limit @max_rows;

-- name: WorkloadView :one
select w.id, a.name as app_name, w.name, w.kind, w.desired_state, a.state as app_state,
       r.version, r.id as release_id, w.created_at, r.created_at as deployed_at
from workloads w
join apps a on a.id = w.app_id
left join releases r on r.id = w.active_release_id
where a.workspace_id = @workspace_id and w.id = @id;

-- name: LockWorkload :one
-- A paused or deleted app still owns its workloads; deletion is checked by
-- the caller.
select w.id, w.desired_state, w.active_release_id, a.state as app_state
from workloads w
join apps a on a.id = w.app_id
where a.workspace_id = @workspace_id and w.id = @id
for update of w;

-- name: SetWorkloadState :exec
update workloads
set desired_state = @desired_state,
    deleted_at = case when @desired_state = 'deleted' then now() else deleted_at end
where id = @id;

-- name: ReleaseByVersion :one
select id from releases where workload_id = @workload_id and version = @version;

-- name: SetActiveRelease :exec
update workloads set active_release_id = @release_id::uuid where id = @id;

-- name: ListVersions :many
-- Deployed versions, newest first, below the cursor version.
select r.id, r.version::int as version, r.created_at, (r.id = w.active_release_id)::bool as active
from releases r
join workloads w on w.id = r.workload_id
where r.workload_id = @workload_id and r.version is not null and r.version < @before
order by r.version desc
limit @max_rows;

-- name: PlanWorkloads :many
-- The app's live functions that are deployed, with how many versions each has.
select w.kind, w.name,
       (select count(*) from releases r where r.workload_id = w.id and r.version is not null)::int as versions
from workloads w
where w.app_id = @app_id and w.desired_state <> 'deleted' and w.active_release_id is not null
order by w.kind, w.name;
