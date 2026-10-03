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
      and coalesce((r.spec -> 'autoscaler' ->> 'min_containers')::int, 0) > 0
),
batch as (
    select release_id from candidates order by release_id limit @batch_size
)
select r.id as release_id,
       a.workspace_id,
       w.id as workload_id,
       (r.version < 0)::bool as preview,
       (w.active_release_id is not distinct from r.id and w.desired_state = 'active' and a.state = 'active')::bool as active,
       (w.desired_state <> 'active' or a.state <> 'active' or ws.state <> 'active')::bool as stopping,
       coalesce((r.spec -> 'autoscaler' ->> 'min_containers')::int, 0)::int as min_containers,
       coalesce((r.spec -> 'autoscaler' ->> 'max_containers')::int, 1)::int as max_containers,
       coalesce((r.spec -> 'autoscaler' ->> 'tasks_per_container')::int, 1)::int as tasks_per_container,
       coalesce((r.spec -> 'http' ->> 'workers')::int, (r.spec ->> 'concurrency')::int, 1)::int as slots,
       (r.spec -> 'resources' ->> 'cpu_millis')::bigint as cpu_millis,
       ((r.spec -> 'resources' ->> 'memory_mib')::bigint * 1048576)::bigint as memory_bytes,
       -- What billing prices and admits, as planning reads it for functions.
       greatest(coalesce((r.spec -> 'resources' ->> 'gpu_count')::int, 0),
                case when jsonb_array_length(coalesce(r.spec -> 'resources' -> 'gpu', '[]'::jsonb)) > 0 then 1 else 0 end)::int
           as gpu_count,
       coalesce(array(select jsonb_array_elements_text(r.spec -> 'resources' -> 'gpu')), '{}')::text[] as gpu_models,
       coalesce((r.spec -> 'placement' ->> 'preemptible')::boolean, true)::bool as preemptible,
       (coalesce(r.spec -> 'placement' ->> 'region', '') <> ''
        or coalesce(r.spec -> 'placement' ->> 'availability_zone', '') <> '')::bool as pinned,
       (coalesce(r.spec -> 'placement' ->> 'machine', '') <> '')::bool as machine,
       demand.current::int as demand,
       demand.peak::int as peak,
       coalesce(p.stopped_at is null and p.lease_expires_at > now() and (p.deadline_at is null or p.deadline_at > now()), false)::bool as preview_live,
       exists (
           select 1 from containers ac
           where ac.release_id = w.active_release_id and ac.state = 'ready' and ac.purpose = 'serve'
       )::bool as active_ready,
       (r.load_error is not null or r.start_failures >= @start_failure_limit::int)::bool as failed,
       c.pending::int as pending,
       c.starting::int as starting,
       c.ready::int as ready,
       c.draining::int as draining
from batch
join releases r on r.id = batch.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
left join previews p on p.release_id = r.id
cross join lateral (
    select coalesce(sum(l.in_flight + l.waiting), 0) as current, coalesce(sum(l.window_peak), 0) as peak
    from endpoint_loads l where l.release_id = r.id and l.expires_at > now()
) demand
cross join lateral (
    select count(*) filter (where c.state = 'pending') as pending,
           count(*) filter (where c.state = 'starting') as starting,
           count(*) filter (where c.state = 'ready') as ready,
           count(*) filter (where c.state = 'draining') as draining
    from containers c where c.release_id = r.id and c.state <> 'stopped' and c.purpose = 'serve'
) c
order by r.id;

-- name: DrainNewestContainers :many
-- Ready and starting containers of the release, newest first: the oldest
-- are the warmest. Their supervisors finish requests in flight before they
-- exit, and the edge stops routing to them when it sees the drain.
update containers
set state = 'draining', drain_started_at = now()
where id in (
    select d.id from containers d
    where d.release_id = @release_id::uuid and d.state in ('starting', 'ready') and d.purpose = 'serve'
    order by d.created_at desc, d.id desc
    limit @count
    for update skip locked
)
  and state in ('starting', 'ready')
returning id, host_id;

-- name: UpsertEndpointLoads :exec
insert into endpoint_loads (release_id, edge_id, in_flight, waiting, window_peak, updated_at, expires_at)
select unnest(@release_ids::uuid[]), @edge_id, unnest(@in_flight::int[]), unnest(@waiting::int[]), unnest(@window_peak::int[]),
       now(), now() + make_interval(secs => @ttl_seconds::float8)
on conflict (release_id, edge_id) do update
set in_flight = excluded.in_flight, waiting = excluded.waiting, window_peak = excluded.window_peak,
    updated_at = excluded.updated_at, expires_at = excluded.expires_at;

-- name: DeleteExpiredEndpointLoads :exec
delete from endpoint_loads where expires_at < now() - interval '1 hour';

-- name: EndpointContainers :many
-- Ready containers of every release of the workload, newest version first.
select r.id as release_id, r.version, c.id as container_id, c.host_id, c.state
from releases r
join containers c on c.release_id = r.id
where r.workload_id = @workload_id and c.state = 'ready' and c.purpose = 'serve' and r.version is not null
order by r.version desc, c.ready_at, c.id;

-- name: ReleaseFailures :many
-- Why containers of these releases cannot start: the handler failed to load
-- since one was last ready, or preparation failed too many times in a row.
select id, coalesce(load_error, '')::text as load_error, start_failures
from releases
where id = any(@ids::uuid[]) and (load_error is not null or start_failures >= @start_failure_limit::int);
