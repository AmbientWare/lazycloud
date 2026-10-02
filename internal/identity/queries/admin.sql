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
