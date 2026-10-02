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

-- name: SecretUsers :many
-- Workloads whose active release receives one of the named secrets.
select s.name::text as secret, a.name as app, w.kind, w.name as workload
from apps a
join workloads w on w.app_id = a.id
join releases r on r.id = w.active_release_id
cross join lateral jsonb_array_elements_text(coalesce(r.spec -> 'secrets', '[]'::jsonb)) as s(name)
where a.workspace_id = @workspace_id and a.state <> 'deleted' and w.desired_state = 'active'
  and s.name = any(@names::text[])
order by a.name, w.kind, w.name;
