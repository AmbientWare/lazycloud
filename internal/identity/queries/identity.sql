-- name: AuthenticateToken :one
select t.id, t.user_id, t.workspace_id, u.email, u.is_admin
from api_tokens t
join users u on u.id = t.user_id
where t.token_hash = @token_hash
  and t.revoked_at is null
  and (t.expires_at is null or t.expires_at > now())
  and u.status = 'active';

-- name: WorkspaceAccess :one
-- The workspace and the user's role in it, empty when not a member.
select w.id, w.name, w.state, w.created_at, coalesce(m.role, '')::text as role
from workspaces w
left join workspace_members m on m.workspace_id = w.id and m.user_id = @user_id
where w.name = @name;

-- name: InsertUser :one
insert into users (email, is_admin) values (@email, @is_admin) returning id;

-- name: UserByEmail :one
select id, email, is_admin from users where email = @email;

-- name: InsertWorkspace :one
insert into workspaces (name) values (@name) returning id, created_at;

-- name: SetWorkspaceConnection :exec
-- Places a new workspace in a connected AWS account; it stays there.
update workspaces set connection_id = @connection_id where id = @id;

-- name: InsertMember :exec
insert into workspace_members (workspace_id, user_id, role) values (@workspace_id, @user_id, @role);

-- name: WorkspaceByName :one
select id, name from workspaces where name = @name;

-- name: InsertToken :one
insert into api_tokens (user_id, workspace_id, name, token_hash, expires_at, device)
values (@user_id, sqlc.narg(workspace_id), @name, @token_hash, sqlc.narg(expires_at), @device)
returning id, name, workspace_id, device, created_at, expires_at, last_used_at;
