-- name: PlanningReleases :many
-- Releases after @after_id that need a planning decision: queued or running
-- tasks, live containers, or a warm minimum on an active release. Each source
-- reads a partial index of live rows, so retained history costs nothing. The
-- queued releases are found by skipping through tasks_queued one release at
-- a time, so a deep backlog costs one probe per release, not per task.
with recursive queued (release_id) as (
    (select t.release_id from tasks t
     where t.status = 'queued' and t.release_id > @after_id
     order by t.release_id limit 1)
    union all
    select (select t.release_id from tasks t
            where t.status = 'queued' and t.release_id > q.release_id
            order by t.release_id limit 1)
    from queued q where q.release_id is not null
), candidates as (
    select release_id from queued where release_id is not null
    union
    select t.release_id from tasks t where t.status = 'running' and t.release_id > @after_id
    union
    select c.release_id from containers c where c.state <> 'stopped' and c.purpose = 'serve' and c.release_id > @after_id
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
    where cw.kind = 'function' and (cr.version is null or cr.version > 0)
    order by c.release_id
    limit @batch_size
)
select r.id as release_id,
       a.workspace_id,
       (w.active_release_id is not distinct from r.id and w.desired_state = 'active' and a.state = 'active'
        and ws.state = 'active')::bool as active,
       -- A deleted app or workload and a deleting workspace wind down; a
       -- paused app's or stopped workload's deployed versions do too, while
       -- their working-tree releases keep running.
       (a.state = 'deleted' or w.desired_state = 'deleted' or ws.state = 'deleting'
        or (r.version is not null and (a.state <> 'active' or w.desired_state = 'stopped')))::bool as stopping,
       -- Deletion also cancels running tasks.
       (a.state = 'deleted' or w.desired_state = 'deleted')::bool as retiring,
       coalesce((r.spec -> 'autoscaler' ->> 'min_containers')::int, 0)::int as min_containers,
       coalesce((r.spec -> 'autoscaler' ->> 'max_containers')::int, 1)::int as max_containers,
       coalesce((r.spec -> 'autoscaler' ->> 'tasks_per_container')::int, 1)::int as tasks_per_container,
       coalesce((r.spec ->> 'concurrency')::int, 1)::int as slots,
       coalesce((r.spec ->> 'keep_warm_seconds')::int, 10)::int as keep_warm_seconds,
       (r.spec -> 'resources' ->> 'cpu_millis')::bigint as cpu_millis,
       ((r.spec -> 'resources' ->> 'memory_mib')::bigint * 1048576)::bigint as memory_bytes,
       -- What billing prices and admits: the cards each container holds (a
       -- GPU list without a count holds one), and whether placement is
       -- preemptible and pinned to a region, zone or machine.
       greatest(coalesce((r.spec -> 'resources' ->> 'gpu_count')::int, 0),
                case when jsonb_array_length(coalesce(r.spec -> 'resources' -> 'gpu', '[]'::jsonb)) > 0 then 1 else 0 end)::int
           as gpu_count,
       coalesce(array(select jsonb_array_elements_text(r.spec -> 'resources' -> 'gpu')), '{}')::text[] as gpu_models,
       coalesce((r.spec -> 'placement' ->> 'preemptible')::boolean, true)::bool as preemptible,
       (coalesce(r.spec -> 'placement' ->> 'region', '') <> ''
        or coalesce(r.spec -> 'placement' ->> 'availability_zone', '') <> '')::bool as pinned,
       (coalesce(r.spec -> 'placement' ->> 'machine', '') <> '')::bool as machine,
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
-- Demand past max_containers * tasks_per_container changes no decision, so
-- the count stops there and a deep backlog reads a bounded prefix.
cross join lateral (
    select count(*) as available from (
        select 1 from tasks t
        where t.release_id = r.id and t.status = 'queued'
          and t.available_at <= now() and t.unmet_dependencies = 0
        limit greatest(coalesce((r.spec -> 'autoscaler' ->> 'max_containers')::int, 1), 1)::bigint
              * greatest(coalesce((r.spec -> 'autoscaler' ->> 'tasks_per_container')::int, 1), 1)::bigint
    ) capped
) q
cross join lateral (
    select count(*) as running from tasks t where t.release_id = r.id and t.status = 'running'
) run
cross join lateral (
    select count(*) filter (where c.state = 'pending') as pending,
           count(*) filter (where c.state = 'starting') as starting,
           count(*) filter (where c.state = 'ready') as ready,
           count(*) filter (where c.state = 'draining') as draining
    from containers c where c.release_id = r.id and c.state <> 'stopped' and c.purpose = 'serve'
) c
order by r.id;

-- name: CreatePendingContainers :many
-- Placement records the GPU model and whose machine a container got.
insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes, gpu_count, rate_class, traceparent)
select @workspace_id, @release_id::uuid, 'pending', @slots, @cpu_millis, @memory_bytes, @gpu_count, @rate_class, sqlc.narg('traceparent')::text
from generate_series(1, @count::int)
returning id;

-- name: ScaleUpTrace :one
-- The trace a scale-up of the release joins: the queued task that has
-- waited longest, read through tasks_queued; else, while the release rolls
-- out (no live container, created within rollout_seconds), its deploy's.
select coalesce(
    (select t.traceparent from tasks t
     where t.release_id = @release_id::uuid and t.status = 'queued' and t.available_at <= now()
     order by t.available_at, t.id limit 1),
    (select r.traceparent from releases r
     where r.id = @release_id::uuid and @rollout::bool
       and r.created_at > now() - make_interval(secs => @rollout_seconds::float8)),
    ''
)::text as traceparent;

-- name: StopPendingContainers :many
-- Newest first, because the oldest are closest to placement. The state check
-- loses to a concurrent assignment.
update containers
set state = 'stopped', stop_reason = 'stopped', exit_message = 'demand ended', stopped_at = now()
where id in (
    select p.id from containers p
    where p.release_id = @release_id::uuid and p.state = 'pending' and p.purpose = 'serve'
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
  and c.purpose = 'serve'
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
-- The caller holds the rows from LockQueuedWithDependents.
update tasks
set status = 'cancelled', finished_at = now()
where id = any(@ids::uuid[]) and status = 'queued'
returning id;

-- name: RunningTasksOfRelease :many
select id from tasks
where release_id = @release_id and status = 'running'
order by id
limit @batch_size;

-- name: TryPlanningLock :one
-- Serializes planners for the rest of the transaction, so container creation
-- respects max_containers across scheduler replicas.
select pg_try_advisory_xact_lock(hashtextextended('execution-planning', 0))::bool;

-- name: HasLiveWork :one
-- Whether anything exists that time alone can advance: a queued or running
-- task, or a live container (pending, starting, ready or draining). Each
-- check takes the first entry of a partial index of live rows: ordering by
-- the indexed column, which EXISTS would drop, keeps the planner from
-- scanning history for a match.
select ((select 1 from tasks where status = 'queued' order by release_id limit 1) is not null
        or (select 1 from tasks where status = 'running' order by release_id limit 1) is not null
        or (select 1 from containers where state <> 'stopped' order by release_id limit 1) is not null)::bool as live;
