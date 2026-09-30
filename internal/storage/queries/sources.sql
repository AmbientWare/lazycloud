-- name: SourceObjectSize :one
select size_bytes from source_objects where workspace_id = @workspace_id and sha256 = @sha256;

-- name: InsertSourceObject :exec
insert into source_objects (workspace_id, sha256, size_bytes)
values (@workspace_id, @sha256, @size_bytes)
on conflict (workspace_id, sha256) do nothing;
