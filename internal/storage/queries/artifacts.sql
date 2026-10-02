-- name: ArtifactTask :one
-- The task an artifact is saved for, with its app at save time.
select a.id as app_id, a.name as app_name
from tasks t
join workloads w on w.id = t.workload_id
join apps a on a.id = w.app_id
where t.id = @task_id and t.workspace_id = @workspace_id;

-- name: InsertArtifact :one
insert into artifacts (
    id, workspace_id, task_id, app_id, app_name, filename, content_type, size_bytes,
    state, upload_id, retention_seconds
) values (
    @id, @workspace_id, @task_id, @app_id, @app_name, @filename, @content_type, @size_bytes,
    'uploading', sqlc.narg(upload_id), @retention_seconds
)
returning *;

-- name: LockUploadingArtifact :one
select * from artifacts
where id = @id and workspace_id = @workspace_id and state = 'uploading'
for update;

-- name: StoreArtifact :one
update artifacts
set state = 'stored', upload_id = null, stored_at = now(),
    expires_at = now() + make_interval(secs => retention_seconds::float8)
where id = @id
returning *;

-- name: StoredArtifact :one
select * from artifacts
where id = @id and workspace_id = @workspace_id and state = 'stored' and expires_at > now();

-- name: AnyArtifact :one
select * from artifacts where id = @id and workspace_id = @workspace_id;

-- name: ListArtifacts :many
select * from artifacts
where workspace_id = @workspace_id and state = 'stored' and expires_at > now()
  and (sqlc.narg(task_id)::uuid is null or task_id = sqlc.narg(task_id))
  and (sqlc.narg(app_name)::text is null or app_name = sqlc.narg(app_name))
  and (sqlc.narg(search)::text is null or strpos(lower(filename), lower(sqlc.narg(search))) > 0)
  and (sqlc.narg(content_type)::text is null or starts_with(content_type, sqlc.narg(content_type)))
  and (sqlc.narg(created_after)::timestamptz is null or created_at >= sqlc.narg(created_after))
  and (sqlc.narg(created_before)::timestamptz is null or created_at < sqlc.narg(created_before))
  and (sqlc.narg(cursor_at)::timestamptz is null
       or (created_at, id) < (sqlc.narg(cursor_at)::timestamptz, sqlc.narg(cursor_id)::uuid))
order by created_at desc, id desc
limit @max_rows;

-- name: ArtifactSummary :one
select count(*)::bigint as count, coalesce(sum(size_bytes), 0)::bigint as size_bytes
from artifacts
where workspace_id = @workspace_id and state = 'stored' and expires_at > now();

-- name: DeleteArtifacts :many
delete from artifacts
where workspace_id = @workspace_id and id = any(@ids::uuid[])
returning id, upload_id;

-- name: ExpiredArtifacts :many
-- Stored artifacts past retention and uploads abandoned for a day.
select id, workspace_id, upload_id from artifacts
where (state = 'stored' and expires_at <= now())
   or (state = 'uploading' and created_at < now() - interval '1 day')
order by created_at
limit @max_rows;

-- name: DeleteExpiredArtifactRows :exec
-- Rows whose bytes are gone; the condition matches ExpiredArtifacts.
delete from artifacts
where id = any(@ids::uuid[])
  and ((state = 'stored' and expires_at <= now())
       or (state = 'uploading' and created_at < now() - interval '1 day'));

-- name: ArtifactIDs :many
-- The ids among ids that still have a row.
select id from artifacts where id = any(@ids::uuid[]);
