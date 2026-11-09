-- name: ServingReleases :many
-- Releases after @after_id whose containers follow traffic or a preview
-- instead of queued tasks: HTTP workloads and previews with live containers,
-- unexpired edge demand, a live preview or a warm minimum. Each source reads
-- live rows only.
with candidates as (
    select c.release_id
    from containers c
    join releases r on r.id = c.release_id
    join workloads w on w.id = r.workload_id
    where c.state <> 'stopped' and c.purpose = 'serve' and c.release_id > @after_id::uuid
      and (w.kind in ('endpoint', 'asgi') or (w.kind = 'function' and r.version < 0))
    union
    select l.release_id from endpoint_loads l where l.expires_at > now() and l.release_id > @after_id
    union
    select p.release_id from previews p where p.stopped_at is null and p.release_id > @after_id
    union
    select w.active_release_id
    from workloads w
    join apps a on a.id = w.app_id
    join releases r on r.id = w.active_release_id
    where w.kind in ('endpoint', 'asgi')
      and w.desired_state = 'active'
      and a.state = 'active'
      and w.active_release_id > @after_id
      and (r.spec -> 'autoscaler' ->> 'min_containers')::int > 0
),
batch as (
    select release_id from candidates order by release_id limit @batch_size
)
select a.workspace_id,
       (r.version < 0)::bool as preview,
       (w.active_release_id is not distinct from r.id and w.desired_state = 'active' and a.state = 'active')::bool as active,
       (w.desired_state <> 'active' or a.state <> 'active' or ws.state <> 'active')::bool as stopping,
       coalesce((r.spec -> 'autoscaler' ->> 'min_containers')::int, 0)::int as min_containers,
       coalesce((r.spec -> 'autoscaler' ->> 'max_containers')::int, 1)::int as max_containers,
       coalesce((r.spec -> 'autoscaler' ->> 'tasks_per_container')::int, 1)::int as tasks_per_container,
       coalesce((r.spec -> 'http' ->> 'workers')::int, (r.spec ->> 'concurrency')::int, 1)::int as slots,
       sqlc.embed(shape),
       demand.current::int as demand,
       demand.peak::int as peak,
       coalesce(preview_live(p), false)::bool as preview_live,
       exists (
           select 1 from containers ac
           where ac.release_id = w.active_release_id and ac.state = 'ready' and ac.purpose = 'serve'
       )::bool as active_ready,
       (r.load_error is not null or r.start_failures >= @start_failure_limit::int)::bool as failed,
       c.pending::int as pending,
       c.starting::int as starting,
       c.ready::int as ready,
       c.draining::int as draining,
       coalesce(c.leaving, false)::bool as leaving
from batch
join releases r on r.id = batch.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
left join previews p on p.release_id = r.id
join release_shapes shape on shape.id = r.id
cross join lateral (
    select coalesce(sum(l.in_flight + l.waiting), 0) as current, coalesce(sum(l.window_peak), 0) as peak
    from endpoint_loads l where l.release_id = r.id and l.expires_at > now()
) demand
cross join lateral (
    select count(*) filter (where c.state = 'pending') as pending,
           count(*) filter (where c.state = 'starting') as starting,
           count(*) filter (where c.state = 'ready') as ready,
           count(*) filter (where c.state = 'draining') as draining,
           bool_or(c.state in ('starting', 'ready') and container_leaves(c.id, c.host_id, r.spec)) as leaving
    from containers c where c.release_id = r.id and c.state <> 'stopped' and c.purpose = 'serve'
) c
order by r.id;

-- name: UpsertEndpointLoads :exec
insert into endpoint_loads (release_id, edge_id, in_flight, waiting, window_peak, updated_at, expires_at)
select unnest(@release_ids::uuid[]), @edge_id, unnest(@in_flight::int[]), unnest(@waiting::int[]), unnest(@window_peak::int[]),
       now(), now() + make_interval(secs => @ttl_seconds::float8)
on conflict (release_id, edge_id) do update
set in_flight = excluded.in_flight, waiting = excluded.waiting, window_peak = excluded.window_peak,
    updated_at = excluded.updated_at, expires_at = excluded.expires_at;

-- name: DeleteExpiredEndpointLoads :exec
delete from endpoint_loads where expires_at < now() - interval '1 hour';

-- name: ReadyContainers :many
-- Ready serve containers of the workload's releases with their hosts,
-- newest version first and then the oldest container. Reads the
-- containers_workload_live partial index.
select c.id, c.host_id, c.release_id::uuid as release_id, r.version, c.traceparent
from containers c
join releases r on r.id = c.release_id
where c.workload_id = @workload_id::uuid and c.state = 'ready' and c.purpose = 'serve' and c.host_id is not null
order by r.version desc nulls last, c.ready_at, c.id;
