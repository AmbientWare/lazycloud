-- name: ListUserTokens :many
-- Newest first; ids are UUIDv7, so id order is creation order.
select id, name, workspace_id, device, created_at, expires_at, last_used_at
from api_tokens
where user_id = @user_id
  and revoked_at is null
  and (@include_device::bool or not device)
  and (sqlc.narg(before)::uuid is null or id < sqlc.narg(before)::uuid)
order by id desc
limit @row_limit;

-- name: RevokeUserToken :execrows
update api_tokens set revoked_at = now()
where id = @id and user_id = @user_id and revoked_at is null;

-- name: TouchTokens :exec
-- Applies a batch of last-used times; a later time never moves back.
update api_tokens t
set last_used_at = u.used_at
from (select unnest(@ids::uuid[]) as id, unnest(@used_at::timestamptz[]) as used_at) u
where t.id = u.id and (t.last_used_at is null or t.last_used_at < u.used_at);

-- name: RevokeWorkspaceTokens :exec
update api_tokens set revoked_at = now() where workspace_id = @workspace_id and revoked_at is null;
