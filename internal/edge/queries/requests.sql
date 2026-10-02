-- name: InsertRequests :exec
-- One batch of finished requests. A release deleted since is skipped, so
-- the rest of the batch is kept.
insert into http_requests (id, workspace_id, workload_id, release_id, container_id, method, path, status,
                           started_at, duration_ms, request_bytes, response_bytes)
select (@ids::uuid[])[i], (@workspace_ids::uuid[])[i], (@workload_ids::uuid[])[i], r.id,
       nullif((@container_ids::uuid[])[i], '00000000-0000-0000-0000-000000000000'::uuid),
       (@methods::text[])[i], (@paths::text[])[i], (@statuses::int[])[i], (@started_ats::timestamptz[])[i],
       (@durations::bigint[])[i], (@request_bytes::bigint[])[i], (@response_bytes::bigint[])[i]
from generate_subscripts(@ids::uuid[], 1) as i
join releases r on r.id = (@release_ids::uuid[])[i]
on conflict (id) do nothing;

-- name: InsertRequestCallbacks :exec
-- A callback per written request whose release names a callback_url: a
-- client that left (499) cancelled it, a 5xx failed it. A batch written
-- again adds none.
insert into task_callbacks (request_id, workspace_id, url, event, attempt, max_attempts)
select h.id, h.workspace_id, r.spec ->> 'callback_url',
       case when h.status = 499 then 'cancelled' when h.status >= 500 then 'failed' else 'succeeded' end,
       1, 1
from http_requests h
join releases r on r.id = h.release_id
where h.id = any(@ids::uuid[]) and r.spec ->> 'callback_url' is not null
on conflict (request_id) do nothing;

-- name: PruneRequests :execrows
-- The oldest requests past the retention, a bounded batch at a time.
delete from http_requests where id in (
    select old.id from http_requests old where old.started_at < @before
    order by old.started_at
    limit @max_rows
    for update skip locked
);

-- name: ListRequests :many
select h.id, a.name as app_name, w.name, w.kind, h.release_id, rel.version, h.container_id,
       h.method, h.path, h.status, h.started_at, h.duration_ms, h.request_bytes, h.response_bytes
from http_requests h
join workloads w on w.id = h.workload_id
join apps a on a.id = w.app_id
join releases rel on rel.id = h.release_id
where h.workspace_id = @workspace_id and a.name = @app_name and a.state <> 'deleted'
  and (sqlc.narg(name)::text is null or w.name = sqlc.narg(name))
  and (sqlc.narg(before)::uuid is null or h.id < sqlc.narg(before))
order by h.id desc
limit @max_rows;

-- name: GetRequest :one
select h.id, a.name as app_name, w.name, w.kind, h.release_id, rel.version, h.container_id,
       h.method, h.path, h.status, h.started_at, h.duration_ms, h.request_bytes, h.response_bytes
from http_requests h
join workloads w on w.id = h.workload_id
join apps a on a.id = w.app_id
join releases rel on rel.id = h.release_id
where h.id = @id and h.workspace_id = @workspace_id;
