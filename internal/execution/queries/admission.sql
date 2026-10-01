-- name: LockFunctionForSubmit :one
-- The workload lock makes the max_pending_tasks count exact for this submit.
-- Without a release id the task runs on the active release.
select w.id, w.name, w.desired_state, a.name as app_name, a.state as app_state,
       r.id as release_id, r.version, r.spec,
       coalesce(p.stopped_at is null and p.lease_expires_at > now()
                and (p.deadline_at is null or p.deadline_at > now()), false)::bool as preview_live
from workloads w
join apps a on a.id = w.app_id
join releases r on r.id = coalesce(sqlc.narg(release_id)::uuid, w.active_release_id) and r.workload_id = w.id
left join previews p on p.release_id = r.id
where a.workspace_id = @workspace_id and a.name = @app_name and a.state <> 'deleted'
  and w.kind = 'function' and w.name = @name and w.desired_state <> 'deleted'
for update of w;

-- name: CountQueuedTasks :one
select count(*) from tasks where workload_id = @workload_id and status = 'queued';

-- name: LockUpstreamTasks :many
-- FOR SHARE holds each upstream's status until the submit commits, so an
-- upstream either finished before and is read here, or finishes after and
-- sees the new dependency rows.
-- A succeeded upstream reports the size of the result it hands on.
select t.id, t.status, coalesce(octet_length(r.data), 0)::bigint as result_bytes
from tasks t
left join task_results r on r.task_id = t.id
where t.id = any(@ids::uuid[]) and t.workspace_id = @workspace_id
order by t.id
for share of t;

-- name: ParentTask :one
select id, root_task_id from tasks where id = @id and workspace_id = @workspace_id;

-- name: InsertTasks :many
-- One statement inserts every task and its input; rows return in input order.
with input as materialized (
    select uuidv7() as id, i as ord, (@encodings::text[])[i] as encoding, (@data::bytea[])[i] as data,
           (@unmet::int[])[i] as unmet
    from generate_subscripts(@encodings::text[], 1) as i
), task as (
    insert into tasks (id, workspace_id, workload_id, release_id, status, max_attempts,
                       parent_task_id, root_task_id, unmet_dependencies, scheduled_for)
    select input.id, @workspace_id, @workload_id, @release_id, 'queued', @max_attempts,
           sqlc.narg(parent_task_id)::uuid, sqlc.narg(root_task_id)::uuid, input.unmet,
           sqlc.narg(scheduled_for)::timestamptz
    from input
    order by input.ord
    returning tasks.id, tasks.created_at
), task_input as (
    insert into task_inputs (task_id, encoding, data)
    select input.id, input.encoding, input.data from input
)
select task.id, task.created_at
from input
join task on task.id = input.id
order by input.ord;

-- name: InsertDependencies :exec
insert into task_dependencies (task_id, depends_on)
select unnest(@task_ids::uuid[]), unnest(@depends_on::uuid[]);
