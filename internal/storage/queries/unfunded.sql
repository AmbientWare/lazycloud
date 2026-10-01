-- name: MarkWorkspaceVolumesDeleting :execrows
-- Volumes no live container mounts; the sweep removes their files.
update volumes v set state = 'deleting', deleted_at = now()
where v.workspace_id = @workspace_id and v.state = 'active'
  and not exists (
      select 1 from volume_mounts vm join containers c on c.id = vm.container_id
      where vm.volume_id = v.id and c.state <> 'stopped'
  );

-- name: MarkWorkspaceDisksDeleting :execrows
-- Disks no container holds; the sweep removes their bytes.
update disks set state = 'deleting', deleted_at = now(), updated_at = now()
where workspace_id = @workspace_id and state = 'active' and holder_container_id is null;

-- name: WorkspaceArtifacts :many
select id from artifacts where workspace_id = @workspace_id order by id limit @row_limit;
