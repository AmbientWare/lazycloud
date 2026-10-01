-- name: TaskStatus :one
select status from tasks where id = @id and workspace_id = @workspace_id;

-- name: WorkloadInWorkspace :one
select w.id from workloads w join apps a on a.id = w.app_id
where w.id = @id and a.workspace_id = @workspace_id;

-- name: ContainerStateInWorkspace :one
select state from containers where id = @id and workspace_id = @workspace_id;

-- Readers page by (writer, id) and stop at the first unsettled line: one
-- whose writer is not below the oldest running transaction, so an earlier
-- line may still commit.

-- name: LogCursor :one
-- The (writer, id) position of an entry id a client resumes after.
select writer::text::bigint as writer from task_logs where id = @id;

-- name: TaskLogsAfter :many
select id, task_id, attempt, stream, data, logged_at, writer::text::bigint as writer,
       (writer < pg_snapshot_xmin(pg_current_snapshot()))::bool as settled
from task_logs
where task_id = @key and (writer, id) > ((@after_writer::bigint)::text::xid8, @after_id::bigint)
order by writer, id
limit @max_entries;

-- name: WorkloadLogsAfter :many
select id, task_id, attempt, stream, data, logged_at, writer::text::bigint as writer,
       (writer < pg_snapshot_xmin(pg_current_snapshot()))::bool as settled
from task_logs
where workload_id = @key and (writer, id) > ((@after_writer::bigint)::text::xid8, @after_id::bigint)
order by writer, id
limit @max_entries;

-- name: ContainerLogsAfter :many
select id, task_id, attempt, stream, data, logged_at, writer::text::bigint as writer,
       (writer < pg_snapshot_xmin(pg_current_snapshot()))::bool as settled
from task_logs
where container_id = @key and (writer, id) > ((@after_writer::bigint)::text::xid8, @after_id::bigint)
order by writer, id
limit @max_entries;

-- name: TaskLogTail :one
-- The position of the oldest of the last @tail lines.
select writer::text::bigint as writer, id from task_logs
where task_id = @key
order by writer desc, id desc
offset @tail - 1
limit 1;

-- name: WorkloadLogTail :one
-- The position of the oldest of the last @tail lines.
select writer::text::bigint as writer, id from task_logs
where workload_id = @key
order by writer desc, id desc
offset @tail - 1
limit 1;

-- name: ContainerLogTail :one
-- The position of the oldest of the last @tail lines.
select writer::text::bigint as writer, id from task_logs
where container_id = @key
order by writer desc, id desc
offset @tail - 1
limit 1;

-- name: InsertLogs :many
-- Lines keep their order. Lines for attempts outside the container or host
-- are dropped. Returns the tasks and the workload that received lines.
with line as (
    select i as ord, (@attempt_ids::uuid[])[i] as attempt_id, (@streams::text[])[i] as stream,
           (@data::text[])[i] as data, (@logged_at::timestamptz[])[i] as logged_at
    from generate_subscripts(@attempt_ids::uuid[], 1) as i
), inserted as (
    insert into task_logs (task_id, attempt, stream, data, logged_at, workload_id, container_id)
    select a.task_id, a.number, line.stream, line.data, line.logged_at, r.workload_id, c.id
    from line
    join attempts a on a.id = line.attempt_id
    join containers c on c.id = a.container_id
    join releases r on r.id = c.release_id
    where c.id = @container_id and c.host_id = @host_id
    order by line.ord
    returning task_id, workload_id
)
select distinct task_id, workload_id from inserted;
