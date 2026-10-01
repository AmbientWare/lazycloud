-- name: InsertSecret :one
insert into secrets (workspace_id, name, key_id, wrapped_key, nonce, ciphertext)
values (@workspace_id, @name, @key_id, @wrapped_key, @nonce, @ciphertext)
on conflict (workspace_id, name) do nothing
returning name, created_at, updated_at;

-- name: UpsertSecret :one
insert into secrets (workspace_id, name, key_id, wrapped_key, nonce, ciphertext)
values (@workspace_id, @name, @key_id, @wrapped_key, @nonce, @ciphertext)
on conflict (workspace_id, name) do update
set key_id = excluded.key_id,
    wrapped_key = excluded.wrapped_key,
    nonce = excluded.nonce,
    ciphertext = excluded.ciphertext,
    updated_at = now()
returning name, created_at, updated_at;

-- name: UpdateSecret :one
update secrets
set key_id = @key_id, wrapped_key = @wrapped_key, nonce = @nonce, ciphertext = @ciphertext, updated_at = now()
where workspace_id = @workspace_id and name = @name
returning name, created_at, updated_at;

-- name: SecretMetadata :one
select name, created_at, updated_at from secrets where workspace_id = @workspace_id and name = @name;

-- name: SealedSecrets :many
select name, key_id, wrapped_key, nonce, ciphertext, created_at, updated_at
from secrets
where workspace_id = @workspace_id and name = any(@names::text[]);

-- name: ListSecrets :many
select name, created_at, updated_at
from secrets
where workspace_id = @workspace_id and name > @after
order by name
limit @max_rows;

-- name: DeleteSecret :execrows
delete from secrets where workspace_id = @workspace_id and name = @name;
