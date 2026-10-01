-- name: FunctionReleaseByVersion :one
select r.id
from releases r
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
where a.workspace_id = @workspace_id and a.name = @app_name and a.state <> 'deleted'
  and w.kind = 'function' and w.name = @name and w.desired_state <> 'deleted'
  and r.version = @version;
