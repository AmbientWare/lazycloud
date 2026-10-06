-- name: BuildingWorkspaceBuilds :many
-- Running builds the workspace started; their context archive is the
-- workspace's, so they cannot outlive it.
select id, image_digest from image_builds
where workspace_id = @workspace_id and state = 'building'
order by id;

-- name: HandOffWorkspaceBuilds :exec
-- Finished builds pass to another active workspace that resolved the same
-- image, so its build history survives the starting workspace's deletion.
-- Builds nobody else can see are left to be deleted with the workspace.
update image_builds b
set workspace_id = heir.workspace_id
from (
    select distinct on (wi.image_digest) wi.image_digest, wi.workspace_id
    from workspace_images wi
    join workspaces w on w.id = wi.workspace_id
    where wi.image_digest in (select image_digest from image_builds where workspace_id = @workspace_id and state <> 'building')
      and wi.workspace_id <> @workspace_id and w.state = 'active'
    order by wi.image_digest, wi.created_at, wi.workspace_id
) heir
where b.workspace_id = @workspace_id and b.state <> 'building' and b.image_digest = heir.image_digest;
