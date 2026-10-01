-- name: ListUsers :many
-- Accounts by id after after_id, narrowed by a case-insensitive LIKE pattern
-- on the display name, email or GitHub login, the administrator flag and the
-- status. Ids are UUIDv7, so id order is creation order.
select id, email, display_name, avatar_url, github_login, is_admin, status, created_at
from users
where (sqlc.narg(after_id)::uuid is null or id > sqlc.narg(after_id)::uuid)
  and (sqlc.narg(pattern)::text is null
       or display_name ilike sqlc.narg(pattern)::text
       or email ilike sqlc.narg(pattern)::text
       or github_login ilike sqlc.narg(pattern)::text)
  and (sqlc.narg(is_admin)::bool is null or is_admin = sqlc.narg(is_admin)::bool)
  and (sqlc.narg(status)::text is null or status = sqlc.narg(status)::text)
order by id
limit @row_limit;

-- name: LockUsers :many
-- Locks the acting administrator and the account it changes, in id order so
-- two administrators changing each other wait instead of deadlocking.
select id, is_admin, status from users where id = any(@ids::uuid[]) order by id for update;

-- name: SetUserAdmin :exec
update users set is_admin = @is_admin, updated_at = now() where id = @id;

-- name: SetUserStatus :exec
update users set status = @status, updated_at = now() where id = @id;

-- name: RevokeUserTokens :exec
update api_tokens set revoked_at = now() where user_id = @user_id and revoked_at is null;

-- name: DeleteUserSessions :exec
delete from sessions where user_id = @user_id;

-- name: ExpireUserDeviceCodes :exec
-- An approved code not yet collected would mint a token on its next poll.
update device_codes set expires_at = now()
where user_id = @user_id and consumed_at is null and expires_at > now();
