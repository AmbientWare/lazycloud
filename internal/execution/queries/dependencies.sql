-- name: LockQueuedDependents :many
-- Queued tasks that depend directly on any upstream, locked in id order so
-- concurrent upstream outcomes never deadlock on shared dependents. The
-- array is evaluated first, so tasks are read by key and the cost follows
-- the dependents, never the queued backlog.
select t.id from tasks t
where t.id = any(array(select d.task_id from task_dependencies d where d.depends_on = any(@upstream::uuid[])))
  and t.status = 'queued'
order by t.id
for update;

-- name: SatisfyDependencies :many
-- Each upstream succeeds once, so each dependency row counts once.
update tasks t
set unmet_dependencies = t.unmet_dependencies - c.n
from (
    select d.task_id, count(*)::int as n
    from task_dependencies d
    where d.depends_on = any(@upstream::uuid[])
    group by d.task_id
) c
where t.id = c.task_id and t.id = any(@dependents::uuid[]) and t.status = 'queued'
returning t.id, t.release_id, t.unmet_dependencies,
          (select octet_length(i.data) from task_inputs i where i.task_id = t.id)::bigint
          + coalesce((select sum(octet_length(r.data))
                      from task_dependencies d
                      join task_results r on r.task_id = d.depends_on
                      where d.task_id = t.id), 0)::bigint as input_bytes;

-- name: LockDependentClosure :many
-- Queued tasks that depend on any upstream directly or through other tasks,
-- locked in id order.
with recursive closure (id) as (
    select d.task_id from task_dependencies d where d.depends_on = any(@upstream::uuid[])
    union
    select d.task_id from task_dependencies d join closure c on d.depends_on = c.id
)
select t.id, t.release_id from tasks t
where t.id = any(array(select id from closure)) and t.status = 'queued'
order by t.id
for update;

-- name: FailTasks :exec
update tasks
set status = 'failed', failure = @failure, finished_at = now(), current_attempt_id = null
where id = any(@ids::uuid[]) and status = 'queued';

-- name: DependencyResults :many
-- The upstream results of claimed tasks, for the runner to resolve.
select d.task_id, d.depends_on, r.encoding, r.data
from task_dependencies d
join task_results r on r.task_id = d.depends_on
where d.task_id = any(@task_ids::uuid[])
order by d.task_id, d.depends_on;

-- name: LockQueuedWithDependents :many
-- Up to batch_size queued tasks of the release, and every queued task that
-- depends on them directly or transitively, locked together in id order, so
-- failing or cancelling the batch and then its dependents never takes a
-- second round of locks. Callers repeat until no batch is left, so the batch
-- is any queued tasks the tasks_queued index yields, not the lowest ids.
with recursive base as (
    select q.id from tasks q
    where q.release_id = @release_id::uuid and q.status = 'queued'
    limit @batch_size
), closure (id) as (
    select id from base
    union
    select d.task_id from task_dependencies d join closure c on d.depends_on = c.id
)
select t.id, (t.id = any(array(select id from base)))::bool as in_release
from tasks t
where t.id = any(array(select id from closure)) and t.status = 'queued'
order by t.id
for update of t;
