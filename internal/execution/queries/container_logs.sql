-- name: InsertContainerLogs :many
-- Lines in order, only for a container assigned to the calling host.
-- Returns the container's release when lines were stored.
-- A nil request id means the line belongs to no request.
with line as (
    select i as ord, (@streams::text[])[i] as stream, (@data::text[])[i] as data,
           (@logged_at::timestamptz[])[i] as logged_at,
           nullif((@requests::uuid[])[i], '00000000-0000-0000-0000-000000000000'::uuid) as request_id
    from generate_subscripts(@streams::text[], 1) as i
), inserted as (
    insert into container_logs (container_id, stream, data, logged_at, request_id)
    select c.id, line.stream, line.data, line.logged_at, line.request_id
    from line
    join containers c on c.id = @container_id and c.host_id = @host_id
    order by line.ord
    returning container_id
)
select distinct c.release_id::uuid from inserted join containers c on c.id = inserted.container_id where c.release_id is not null;

-- name: ReleaseLogsAfter :many
-- Output of every container of the release, which is how a preview's
-- output continues across a replaced container.
select l.id, l.stream, l.data, l.logged_at
from containers c
join container_logs l on l.container_id = c.id
where c.release_id = @release_id::uuid and l.id > @after
order by l.id
limit @max_entries;

-- name: ReleaseContainer :one
-- The newest live container of the release.
select id, state, host_id from containers
where release_id = @release_id::uuid and state <> 'stopped'
order by created_at desc, id desc
limit 1;

-- name: RequestLogsAfter :many
-- What a workspace's container wrote while serving one request.
select l.id, l.stream, l.data, l.logged_at
from container_logs l
join containers c on c.id = l.container_id
where l.request_id = @request_id and c.workspace_id = @workspace_id and l.id > @after
order by l.id
limit @max_entries;

-- name: PruneContainerLogs :execrows
-- The oldest lines past the retention, a bounded batch at a time.
delete from container_logs where id in (
    select old.id from container_logs old where old.logged_at < @before
    order by old.logged_at
    limit @max_rows
    for update skip locked
);
