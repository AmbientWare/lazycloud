-- name: ListWorkspaces :many
-- Workspaces by name after after_name, with the user's role where a member.
-- members_only limits the list to memberships; restrict_to to one workspace.
select w.id, w.name, w.state, w.created_at, coalesce(m.role, '')::text as role
from workspaces w
left join workspace_members m on m.workspace_id = w.id and m.user_id = @user_id
where (not @members_only::bool or m.user_id is not null)
  and (sqlc.narg(after_name)::text is null or w.name > sqlc.narg(after_name)::text)
  and (sqlc.narg(restrict_to)::uuid is null or w.id = sqlc.narg(restrict_to)::uuid)
order by w.name
limit @row_limit;

-- name: RenameWorkspace :one
update workspaces set name = @name, updated_at = now()
where id = @id and state = 'active'
returning id, name, state, created_at;

-- name: LockWorkspacesForDeletion :exec
-- Serializes deletions so the last active workspace cannot be deleted by two
-- concurrent requests that each see the other's target still active.
select pg_advisory_xact_lock(hashtextextended('workspace-deletion', 0));

-- name: LockWorkspaceByName :one
select id, name, state, created_at from workspaces where name = @name for update;

-- name: CountOtherActiveWorkspaces :one
select count(*)::int from workspaces where state = 'active' and id <> @id;

-- name: MarkWorkspaceDeleting :one
update workspaces set state = 'deleting', deletion_requested_at = now(), updated_at = now()
where id = @id
returning id, name, state, created_at;

-- name: DeleteWorkspaceInvitations :many
delete from invitations where workspace_id = @workspace_id returning message_id;

-- name: DeletingWorkspaces :many
select id, name, deletion_requested_at::timestamptz as deletion_requested_at
from workspaces
where state = 'deleting'
order by deletion_requested_at, id
limit @row_limit;

-- name: DeleteDeletingWorkspace :execrows
delete from workspaces where id = @id and state = 'deleting';
