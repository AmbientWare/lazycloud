-- name: RunningWorkspaceTasks :many
-- Reached through the workspace's releases, so it reads the running-task
-- partial index rather than the task history.
select t.id
from apps a
join workloads w on w.app_id = a.id
join releases r on r.workload_id = w.id
join tasks t on t.release_id = r.id
where a.workspace_id = @workspace_id and t.status = 'running'
order by t.id
limit @row_limit;

-- name: LiveWorkspaceContainers :one
-- Workload and image build containers alike.
select count(*)::int from containers where workspace_id = @workspace_id and state <> 'stopped';
