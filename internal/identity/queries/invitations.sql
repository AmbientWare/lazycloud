-- name: InsertInvitation :one
insert into invitations (workspace_id, email, role, invited_by, token_hash, message_id, expires_at)
values (@workspace_id, @email, @role, @invited_by, @token_hash, @message_id, @expires_at)
on conflict (workspace_id, email) do nothing
returning id, created_at, updated_at;

-- name: LockInvitation :one
select i.id, i.email, i.role, i.invited_by, i.message_id, i.created_at,
       coalesce(nullif(u.display_name, ''), u.email, '')::text as invited_by_name
from invitations i
left join users u on u.id = i.invited_by
where i.id = @id and i.workspace_id = @workspace_id
for update of i;

-- name: ReissueInvitation :one
update invitations
set token_hash = @token_hash, message_id = @message_id, expires_at = @expires_at, updated_at = now()
where id = @id
returning updated_at;

-- name: DeleteInvitation :exec
delete from invitations where id = @id;

-- name: ListInvitations :many
select i.id, i.email, i.role, i.invited_by, i.message_id, i.expires_at, i.created_at, i.updated_at,
       coalesce(nullif(u.display_name, ''), u.email, '')::text as invited_by_name,
       (i.expires_at <= now())::bool as expired
from invitations i
left join users u on u.id = i.invited_by
where i.workspace_id = @workspace_id
order by i.created_at, i.id;

-- name: InvitationByToken :one
select i.id, i.workspace_id, w.name as workspace_name, i.email, i.role, i.expires_at,
       coalesce(nullif(u.display_name, ''), u.email, '')::text as invited_by_name,
       (i.expires_at <= now())::bool as expired
from invitations i
join workspaces w on w.id = i.workspace_id
left join users u on u.id = i.invited_by
where i.token_hash = @token_hash and w.state = 'active';

-- name: LockInvitationByToken :one
select i.id, i.workspace_id, i.email, i.role, i.message_id, (i.expires_at <= now())::bool as expired
from invitations i
where i.token_hash = @token_hash
for update;
