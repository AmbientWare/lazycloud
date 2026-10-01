-- name: PlanningReleases :many
-- Releases after @after_id that need a planning decision: queued or running
-- tasks, live containers, or a warm minimum on an active release. Each source
-- reads a partial index of live rows, so retained history costs nothing.
with candidates as (
    select t.release_id from tasks t where t.status = 'queued' and t.release_id > @after_id
    union
    select t.release_id from tasks t where t.status = 'running' and t.release_id > @after_id
    union
    select c.release_id from containers c where c.state <> 'stopped' and c.release_id > @after_id
    union
    select w.active_release_id
    from workloads w
    join apps a on a.id = w.app_id
    join workspaces ws on ws.id = a.workspace_id
    join releases r on r.id = w.active_release_id
    where w.desired_state = 'active'
      and a.state = 'active'
      and ws.state = 'active'
      and w.active_release_id > @after_id
      and coalesce((r.spec -> 'autoscaler' ->> 'min_containers')::int, 0) > 0
),
batch as (
    -- HTTP workloads and previews follow traffic instead; ServingReleases
    -- plans them.
    select c.release_id
    from candidates c
    join releases cr on cr.id = c.release_id
    join workloads cw on cw.id = cr.workload_id
    where cw.kind = 'function' and cr.version > 0
    order by c.release_id
    limit @batch_size
)
select r.id as release_id,
       a.workspace_id,
       (w.active_release_id is not distinct from r.id and w.desired_state = 'active' and a.state = 'active'
        and ws.state = 'active')::bool as active,
       -- A deleting workspace winds down like a paused app.
       (w.desired_state = 'stopped' or a.state = 'paused' or ws.state = 'deleting')::bool as stopping,
       coalesce((r.spec -> 'autoscaler' ->> 'min_containers')::int, 0)::int as min_containers,
       coalesce((r.spec -> 'autoscaler' ->> 'max_containers')::int, 1)::int as max_containers,
       coalesce((r.spec -> 'autoscaler' ->> 'tasks_per_container')::int, 1)::int as tasks_per_container,
       coalesce((r.spec ->> 'concurrency')::int, 1)::int as slots,
       coalesce((r.spec ->> 'keep_warm_seconds')::int, 10)::int as keep_warm_seconds,
       (r.spec -> 'resources' ->> 'cpu_millis')::bigint as cpu_millis,
       ((r.spec -> 'resources' ->> 'memory_mib')::bigint * 1048576)::bigint as memory_bytes,
       q.available::int as queued_available,
       run.running::int as running,
       c.pending::int as pending,
       c.starting::int as starting,
       c.ready::int as ready,
       c.draining::int as draining
from batch
join releases r on r.id = batch.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
cross join lateral (
    select count(*) filter (where t.available_at <= now()) as available
    from tasks t where t.release_id = r.id and t.status = 'queued'
) q
cross join lateral (
    select count(*) as running from tasks t where t.release_id = r.id and t.status = 'running'
) run
cross join lateral (
    select count(*) filter (where c.state = 'pending') as pending,
           count(*) filter (where c.state = 'starting') as starting,
           count(*) filter (where c.state = 'ready') as ready,
           count(*) filter (where c.state = 'draining') as draining
    from containers c where c.release_id = r.id and c.state <> 'stopped'
) c
order by r.id;

-- name: CreatePendingContainers :many
insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes)
select @workspace_id, @release_id::uuid, 'pending', @slots, @cpu_millis, @memory_bytes
from generate_series(1, @count::int)
returning id;

-- name: StopPendingContainers :many
-- Newest first, because the oldest are closest to placement. The state check
-- loses to a concurrent assignment.
update containers
set state = 'stopped', stop_reason = 'stopped', exit_message = 'demand ended', stopped_at = now()
where id in (
    select p.id from containers p
    where p.release_id = @release_id::uuid and p.state = 'pending'
    order by p.created_at desc, p.id desc
    limit @count
    for update skip locked
)
  and state = 'pending'
returning id;

-- name: LockIdleContainers :many
-- Ready containers idle for longer than keep_warm_seconds, most idle first.
-- SKIP LOCKED passes over containers a claim holds FOR SHARE: they are about
-- to run a task. DrainIdleContainers rechecks idleness after these locks.
select c.id
from containers c
where c.release_id = @release_id::uuid
  and c.state = 'ready'
  and not exists (select 1 from attempts a where a.container_id = c.id and a.state = 'running')
  and coalesce((select max(a.finished_at) from attempts a where a.container_id = c.id), c.ready_at)
      < now() - make_interval(secs => @keep_warm_seconds::float8)
order by coalesce((select max(a.finished_at) from attempts a where a.container_id = c.id), c.ready_at), c.id
limit @count
for update of c skip locked;

-- name: DrainIdleContainers :many
-- Runs as its own statement after LockIdleContainers so its snapshot includes
-- every claim that committed before the locks were taken.
update containers c
set state = 'draining', drain_started_at = now()
where c.id = any(@ids::uuid[])
  and c.state = 'ready'
  and not exists (select 1 from attempts a where a.container_id = c.id and a.state = 'running')
  and coalesce((select max(a.finished_at) from attempts a where a.container_id = c.id), c.ready_at)
      < now() - make_interval(secs => @keep_warm_seconds::float8)
returning c.id, c.host_id;

-- name: StopAllPendingContainers :many
update containers
set state = 'stopped', stop_reason = 'stopped', exit_message = 'workload stopped', stopped_at = now()
where release_id = @release_id::uuid and state = 'pending'
returning id;

-- name: DrainAllContainers :many
-- Starting and ready containers stop claiming; hosts stop them once their
-- running attempts finish.
update containers
set state = 'draining', drain_started_at = now()
where release_id = @release_id::uuid and state in ('starting', 'ready')
returning id, host_id;

-- name: CancelQueuedTasks :many
update tasks
set status = 'cancelled', finished_at = now()
where id in (
    select q.id from tasks q
    where q.release_id = @release_id::uuid and q.status = 'queued'
    order by q.available_at, q.id
    limit @batch_size
    for update skip locked
)
  and status = 'queued'
returning id;

-- name: TryPlanningLock :one
-- Serializes planners for the rest of the transaction, so container creation
-- respects max_containers across scheduler replicas.
select pg_try_advisory_xact_lock(hashtextextended('execution-planning', 0))::bool;
