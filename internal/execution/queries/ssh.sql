-- name: SSHAuthority :one
select public_key, sealed_key from ssh_authorities where workspace_id = @workspace_id;

-- name: InsertSSHAuthority :exec
-- Concurrent first uses race; the first insert wins and the others read it.
insert into ssh_authorities (workspace_id, public_key, sealed_key) values (@workspace_id, @public_key, @sealed_key)
on conflict (workspace_id) do nothing;

-- name: SSHHostKey :one
select public_key, sealed_key from ssh_host_keys where workload_id = @workload_id;

-- name: InsertSSHHostKey :exec
insert into ssh_host_keys (workload_id, public_key, sealed_key) values (@workload_id, @public_key, @sealed_key)
on conflict (workload_id) do nothing;

-- name: SSHHosts :many
-- Active pods of the workspace that serve SSH, by app then name, after the
-- cursor, with their host key when one exists.
select w.id as workload_id, a.name as app_name, w.name as pod_name,
       coalesce(r.spec -> 'pod' ->> 'kind', 'pod')::text as pod_kind, k.public_key
from workloads w
join apps a on a.id = w.app_id
join releases r on r.id = w.active_release_id
left join ssh_host_keys k on k.workload_id = w.id
where a.workspace_id = @workspace_id and w.kind = 'pod' and w.desired_state = 'active' and a.state = 'active'
  and (coalesce((r.spec -> 'pod' ->> 'ssh')::boolean, false) or r.spec -> 'pod' ->> 'kind' = 'devbox')
  and (sqlc.narg('app')::text is null or a.name = sqlc.narg('app'))
  and (sqlc.narg('pod')::text is null or w.name = sqlc.narg('pod'))
  and (sqlc.narg('role')::text is null or coalesce(r.spec -> 'pod' ->> 'kind', 'pod') = sqlc.narg('role'))
  and (a.name, w.name) > (@after_app::text, @after_pod::text)
order by a.name, w.name
limit @max_rows;

-- name: PodByName :one
-- A pod of an app by name, live or stopped, for a tunnel.
select w.id, w.desired_state, a.state as app_state, r.spec
from workloads w
join apps a on a.id = w.app_id
left join releases r on r.id = w.active_release_id
where a.workspace_id = @workspace_id and a.name = @app and w.kind = 'pod' and w.name = @name
  and w.desired_state <> 'deleted' and a.state <> 'deleted';
