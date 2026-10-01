-- name: LockReleaseForSubmit :one
-- The workload lock makes the max_pending_tasks count exact for this submit.
select w.id as workload_id, w.name, w.kind, w.desired_state, a.name as app_name, a.state as app_state,
       r.id as release_id, r.version, r.spec,
       (p.release_id is not null and p.stopped_at is null and p.lease_expires_at > now()
        and (p.deadline_at is null or p.deadline_at > now()))::bool as preview_live
from releases r
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
left join previews p on p.release_id = r.id
where a.workspace_id = @workspace_id and r.id = @release_id
for update of w;

-- name: FunctionReleaseByVersion :one
select r.id
from releases r
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
where a.workspace_id = @workspace_id and a.name = @app_name and w.kind = 'function' and w.name = @name
  and r.version = @version;
