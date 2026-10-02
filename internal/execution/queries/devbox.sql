-- name: DevboxWorkload :one
-- A pod deployment with its active release and app, in the workspace.
select w.id, w.name, w.kind, w.desired_state, a.name as app_name, a.state as app_state, a.id as app_id,
       w.active_release_id, r.spec,
       (select count(*) from workloads o join apps oa on oa.id = o.app_id
        where oa.workspace_id = a.workspace_id and o.kind = 'pod' and o.name = w.name
          and o.desired_state <> 'deleted' and oa.state <> 'deleted')::int as same_name
from workloads w
join apps a on a.id = w.app_id
left join releases r on r.id = w.active_release_id
where w.id = @id and a.workspace_id = @workspace_id and w.desired_state <> 'deleted';

-- name: DevboxContainer :one
-- The newest live serve container of the workload, with the start stages
-- its host finished and its open connections.
select c.id, c.state, c.host_id, c.keep_warm_seconds, c.active_until, st.stages,
       lease.connections::int as connections,
       coalesce(lease.last_connection, c.created_at)::timestamptz as last_connection
from (
    select c.id, c.state, c.host_id, c.keep_warm_seconds, c.active_until, c.created_at
    from containers c
    join releases r on r.id = c.release_id
    where r.workload_id = @workload_id and c.purpose = 'serve' and c.state <> 'stopped'
    order by c.created_at desc, c.id desc
    limit 1
) c
cross join lateral (
    select coalesce(array_agg(st.stage), '{}')::text[] as stages
    from container_startup_stages st where st.container_id = c.id
) st
cross join lateral (
    select count(*) filter (where l.expires_at > now()) as connections, max(l.expires_at) as last_connection
    from container_leases l where l.container_id = c.id
) lease;

-- name: DevboxFailure :one
-- The newest serve container of the workload that failed to start within
-- the last 15 minutes.
select c.id, coalesce(c.exit_message, c.stop_reason)::text as reason
from containers c
join releases r on r.id = c.release_id
where r.workload_id = @workload_id and c.purpose = 'serve' and c.state = 'stopped'
  and c.stop_reason in ('start_failed', 'crashed', 'out_of_memory', 'load_error')
  and c.stopped_at > now() - interval '15 minutes'
order by c.stopped_at desc
limit 1;

-- name: PodStartFailedSince :one
-- Why the newest serve container of the workload created after @since
-- failed to start, for a connection waiting on it.
select coalesce(c.exit_message, c.stop_reason)::text as reason
from containers c
join releases r on r.id = c.release_id
where r.workload_id = @workload_id and c.purpose = 'serve' and c.created_at >= @since
  and c.state = 'stopped' and c.stop_reason in ('start_failed', 'load_error', 'crashed', 'out_of_memory')
order by c.stopped_at desc
limit 1;
