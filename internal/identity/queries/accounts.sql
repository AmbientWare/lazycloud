-- name: LockGitHubSubject :exec
-- Serializes first sign-ins of one GitHub account, so it gets one user.
select pg_advisory_xact_lock(hashtextextended('github-user:' || @github_user_id::bigint, 0));

-- name: UserByGitHubID :one
select id, status from users where github_user_id = @github_user_id for update;

-- name: UnlinkedUserByEmail :one
-- An account made by the admin command that GitHub has not reached yet.
select id, status from users where lower(email) = lower(@email::text) and github_user_id is null for update;

-- name: InsertGitHubUser :one
-- The account takes the email unless another account already holds it.
insert into users (email, display_name, avatar_url, github_user_id, github_login)
select case when exists (select 1 from users o where o.email = sqlc.narg(email)::text) then null
            else sqlc.narg(email)::text end,
       @display_name, @avatar_url, @github_user_id, @github_login
returning id;

-- name: UpdateGitHubProfile :exec
-- The email follows GitHub's verified primary address unless another
-- account holds it. users.email is unique, so the left join adds at most
-- one row: the other holder, if any.
update users u
set display_name = @display_name, avatar_url = @avatar_url,
    github_user_id = @github_user_id, github_login = @github_login,
    email = case
        when sqlc.narg(email)::text is null or o.id is not null then u.email
        else sqlc.narg(email)::text
    end,
    updated_at = now()
from (select 1) one
left join users o on o.email = sqlc.narg(email)::text and o.id <> @id
where u.id = @id;

-- name: OwnedWorkspace :one
select w.id, w.name
from workspace_members m
join workspaces w on w.id = m.workspace_id
where m.user_id = @user_id and m.role = 'owner'
order by m.created_at
limit 1;

-- name: TakenWorkspaceNames :many
select name from workspaces where name = any(@names::text[]);

-- name: UserProfile :one
select id, email, display_name, avatar_url, github_login, is_admin, status, created_at
from users where id = @id;

-- name: InsertSession :one
insert into sessions (user_id, token_hash, expires_at)
values (@user_id, @token_hash, @expires_at)
returning id;

-- name: AuthenticateSession :one
select s.id, s.user_id, u.email, u.is_admin
from sessions s
join users u on u.id = s.user_id
where s.token_hash = @token_hash and s.expires_at > now() and u.status = 'active';

-- name: DeleteSession :exec
delete from sessions where id = @id;

-- name: PruneSessions :execrows
delete from sessions
where id in (select p.id from sessions p where p.expires_at < now() order by p.expires_at limit @row_limit);
