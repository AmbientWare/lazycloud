-- name: InsertContainerLogs :one
-- Lines in order, only for a container assigned to the calling host.
-- Returns how many were stored.
with line as (
    select i as ord, (@streams::text[])[i] as stream, (@data::text[])[i] as data,
           (@logged_at::timestamptz[])[i] as logged_at
    from generate_subscripts(@streams::text[], 1) as i
), inserted as (
    insert into container_logs (container_id, stream, data, logged_at)
    select c.id, line.stream, line.data, line.logged_at
    from line
    join containers c on c.id = @container_id and c.host_id = @host_id
    order by line.ord
    returning 1
)
select count(*) from inserted;

-- name: ContainerLogsAfter :many
select id, stream, data, logged_at
from container_logs
where container_id = @container_id and id > @after
order by id
limit @max_entries;
