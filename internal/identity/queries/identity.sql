-- name: AuthenticateToken :one
select t.user_id, t.workspace_id, u.email, u.is_admin
from api_tokens t
join users u on u.id = t.user_id
where t.token_hash = @token_hash
  and t.revoked_at is null
  and (t.expires_at is null or t.expires_at > now());

-- name: WorkspaceAccess :one
-- The workspace and whether the user is a member.
select w.id, w.name,
       exists (select 1 from workspace_members m where m.workspace_id = w.id and m.user_id = @user_id) as member
from workspaces w
where w.name = @name;

-- name: MemberWorkspaces :many
-- restrict_to limits the list to one workspace for a restricted token.
select w.id, w.name
from workspace_members m
join workspaces w on w.id = m.workspace_id
where m.user_id = @user_id
  and (sqlc.narg(restrict_to)::uuid is null or w.id = sqlc.narg(restrict_to)::uuid)
order by w.name;

-- name: InsertUser :one
insert into users (email, is_admin) values (@email, @is_admin) returning id;

-- name: UserByEmail :one
select id, email, is_admin from users where email = @email;

-- name: InsertWorkspace :one
insert into workspaces (name) values (@name) returning id;

-- name: InsertMember :exec
insert into workspace_members (workspace_id, user_id, role) values (@workspace_id, @user_id, @role);

-- name: WorkspaceByName :one
select id, name from workspaces where name = @name;

-- name: InsertToken :one
insert into api_tokens (user_id, workspace_id, name, token_hash)
values (@user_id, sqlc.narg(workspace_id), @name, @token_hash)
returning id;
