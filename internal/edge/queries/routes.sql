-- name: Routes :many
-- Every claimed route with its workload, app, workspace and active release.
-- The edge holds them in memory and reloads them on lc_route.
select r.workload_id, r.subdomain, r.hostname,
       coalesce(d.phase = 'ready', false)::bool as hostname_ready,
       w.kind, w.name, w.desired_state,
       a.id as app_id, a.name as app_name, a.state as app_state, a.workspace_id, ws.name as workspace_name,
       rel.id as release_id, rel.version, rel.spec
from http_routes r
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
left join releases rel on rel.id = w.active_release_id
left join custom_domains d on d.hostname = r.hostname and exists (
    select 1 from workspace_members m
    where m.workspace_id = a.workspace_id and m.user_id = d.user_id and m.role = 'owner'
)
where w.desired_state <> 'deleted' and a.state <> 'deleted'
order by r.workload_id;

-- name: ReleaseRoute :one
-- One release with its workload's routing context, for release, version,
-- preview and container hosts.
select rel.id as release_id, rel.version, rel.spec,
       w.id as workload_id, w.kind, w.name, w.desired_state, (w.active_release_id is not distinct from rel.id)::bool as active,
       a.id as app_id, a.name as app_name, a.state as app_state, a.workspace_id, ws.name as workspace_name,
       coalesce(p.stopped_at is null and p.lease_expires_at > now() and (p.deadline_at is null or p.deadline_at > now()), false)::bool as preview_live,
       -- Whether the workload's active release requires a token; every host
       -- of every release follows it, so going private closes old URLs.
       coalesce((ar.spec ->> 'authorized')::bool, ar.id is not null)::bool as active_authorized
from releases rel
join workloads w on w.id = rel.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
left join previews p on p.release_id = rel.id
left join releases ar on ar.id = w.active_release_id
where rel.id = @id;

-- name: WorkloadRoute :one
-- Where a deployed HTTP workload answers: its subdomain and, once ready, its
-- custom hostname.
select r.subdomain, r.hostname, coalesce(d.phase = 'ready', false)::bool as hostname_ready
from http_routes r
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
left join custom_domains d on d.hostname = r.hostname and exists (
    select 1 from workspace_members m
    where m.workspace_id = a.workspace_id and m.user_id = d.user_id and m.role = 'owner'
)
where r.workload_id = @workload_id;

-- name: ReleaseOfVersion :one
select id from releases where workload_id = @workload_id and version = @version;

-- name: ContainerRelease :one
select release_id::uuid from containers where id = @id and state <> 'stopped' and release_id is not null;
