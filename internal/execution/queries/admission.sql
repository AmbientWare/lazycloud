-- name: LockFunctionForSubmit :one
-- The workload lock makes the max_pending_tasks count exact for this submit.
select w.id, w.name, w.desired_state, a.name as app_name, a.state as app_state,
       r.id as release_id, r.spec
from workloads w
join apps a on a.id = w.app_id
join releases r on r.id = w.active_release_id
where a.workspace_id = @workspace_id and a.name = @app_name and w.kind = 'function' and w.name = @name
for update of w;

-- name: CountQueuedTasks :one
select count(*) from tasks where workload_id = @workload_id and status = 'queued';

-- name: InsertTasks :many
-- One statement inserts every task and its input; rows return in input order.
with input as materialized (
    select uuidv7() as id, i as ord, (@encodings::text[])[i] as encoding, (@data::bytea[])[i] as data
    from generate_subscripts(@encodings::text[], 1) as i
), task as (
    insert into tasks (id, workspace_id, workload_id, release_id, status, max_attempts)
    select input.id, @workspace_id, @workload_id, @release_id, 'queued', @max_attempts
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
