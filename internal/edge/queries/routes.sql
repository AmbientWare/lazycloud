-- name: Routes :many
-- Every claimed route with its workload, app, workspace and active release.
-- The edge holds them in memory and reloads them on lc_route.
select r.workload_id, r.subdomain, r.hostname,
       coalesce(d.phase = 'ready', false)::bool as hostname_ready,
       w.kind, w.name, w.desired_state,
       a.name as app_name, a.state as app_state, a.workspace_id, ws.name as workspace_name,
       rel.id as release_id, rel.version, rel.spec
from http_routes r
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
left join releases rel on rel.id = w.active_release_id
left join custom_domains d on d.hostname = r.hostname
order by r.workload_id;

-- name: ReleaseRoute :one
-- One release with its workload's routing context, for release, version,
-- preview and container hosts.
select rel.id as release_id, rel.version, rel.spec,
       w.id as workload_id, w.kind, w.name, w.desired_state, (w.active_release_id is not distinct from rel.id)::bool as active,
       a.name as app_name, a.state as app_state, a.workspace_id, ws.name as workspace_name,
       coalesce(p.stopped_at is null, false)::bool as preview_live
from releases rel
join workloads w on w.id = rel.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
left join previews p on p.release_id = rel.id
where rel.id = @id;

-- name: DescribeWorkload :one
-- A deployed HTTP workload with the active release, or the given version.
select w.name, w.kind, w.desired_state, a.name as app_name, r.subdomain, r.hostname,
       coalesce(d.phase = 'ready', false)::bool as hostname_ready,
       rel.id as release_id, rel.version, rel.spec, rel.created_at
from workloads w
join apps a on a.id = w.app_id
join http_routes r on r.workload_id = w.id
join releases rel on rel.id = coalesce(
    (select v.id from releases v where v.workload_id = w.id and v.version = sqlc.narg(version)::int),
    case when sqlc.narg(version)::int is null then w.active_release_id end)
left join custom_domains d on d.hostname = r.hostname
where a.workspace_id = @workspace_id and a.name = @app_name and w.kind = @kind and w.name = @name;

-- name: ReleaseOfVersion :one
select id from releases where workload_id = @workload_id and version = @version;

-- name: ContainerRelease :one
select release_id from containers where id = @id and state <> 'stopped';
