-- name: LockQueuedDependents :many
-- Queued tasks that depend directly on any upstream, locked in id order so
-- concurrent upstream outcomes never deadlock on shared dependents. Tasks
-- are read by key: OFFSET 0 keeps the status test out of the locking scan,
-- because with it the planner may walk the queued index when statistics
-- taken at an empty queue call it empty. Terminal dependents are locked too
-- and dropped by the outer test.
select l.id from (
    select t.id, t.status from tasks t
    where t.id = any(array(select d.task_id from task_dependencies d where d.depends_on = any(@upstream::uuid[])))
    order by t.id
    offset 0
    for update
) l
where l.status = 'queued';

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
-- locked in id order and read by key, as LockQueuedDependents.
with recursive closure (id) as (
    select d.task_id from task_dependencies d where d.depends_on = any(@upstream::uuid[])
    union
    select d.task_id from task_dependencies d join closure c on d.depends_on = c.id
)
select l.id, l.release_id from (
    select t.id, t.release_id, t.status from tasks t
    where t.id = any(array(select id from closure))
    order by t.id
    offset 0
    for update
) l
where l.status = 'queued';

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
-- is the first queued tasks an index yields, not the lowest ids: unordered,
-- the scan stops after batch_size rows.
with recursive base as (
    select q.id from tasks q
    where q.release_id = @release_id::uuid and q.status = 'queued'
    limit @batch_size
), closure (id) as (
    select id from base
    union
    select d.task_id from task_dependencies d join closure c on d.depends_on = c.id
)
select l.id, l.in_release from (
    select t.id, t.status, (t.id = any(array(select id from base)))::bool as in_release
    from tasks t
    where t.id = any(array(select id from closure))
    order by t.id
    offset 0
    for update of t
) l
where l.status = 'queued';
