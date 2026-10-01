-- name: ListMembers :many
-- Owner first, then by when each joined.
select m.user_id, u.display_name, u.email, m.role, m.created_at
from workspace_members m
join users u on u.id = m.user_id
where m.workspace_id = @workspace_id
order by (m.role = 'owner') desc, m.created_at, m.user_id;

-- name: LockMember :one
select m.role, u.display_name, u.email, m.created_at
from workspace_members m
join users u on u.id = m.user_id
where m.workspace_id = @workspace_id and m.user_id = @user_id
for update of m;

-- name: SetMemberRole :exec
update workspace_members set role = @role where workspace_id = @workspace_id and user_id = @user_id;

-- name: DeleteMember :exec
delete from workspace_members where workspace_id = @workspace_id and user_id = @user_id;

-- name: MemberWithEmail :one
select exists (
    select 1 from workspace_members m join users u on u.id = m.user_id
    where m.workspace_id = @workspace_id and lower(u.email) = @email::text
);

-- name: LockActiveWorkspace :one
-- Fences writes against a deletion beginning beside them.
select id, name, created_at from workspaces where id = @id and state = 'active' for share;
