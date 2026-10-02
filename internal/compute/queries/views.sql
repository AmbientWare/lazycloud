-- name: WorkspaceComputeSummary :one
-- The workspace's location and its capacity: a connected account's cloud
-- hosts, or the hosts running the workspace's containers on platform
-- compute.
with counted as (
    select h.phase, h.state, h.last_seen_at, h.hourly_micros
    from hosts h
    join workspaces w on w.connection_id = h.connection_id
    where w.id = @workspace_id and h.kind = 'connection'
      and (h.phase not in ('deleted', 'failed') or h.phase_at > now() - interval '1 day')
    union all
    select h.phase, h.state, h.last_seen_at, null::bigint
    from hosts h
    join workspaces w on w.id = @workspace_id and w.connection_id is null
    where h.id in (select c.host_id from containers c where c.workspace_id = @workspace_id and c.state <> 'stopped')
)
select cc.aws_account_id, cc.phase as connection_phase,
       hosts.total, hosts.ready, hosts.pending, hosts.degraded, hosts.hourly_micros,
       served.workload_count
from workspaces w
left join cloud_connections cc on cc.id = w.connection_id
cross join (
    select count(*) filter (where phase not in ('deleted'))::int as total,
           count(*) filter (where phase = 'ready' and state = 'online'
               and last_seen_at >= now() - make_interval(secs => @timeout_seconds::float8))::int as ready,
           count(*) filter (where phase in ('requested', 'provisioning', 'booting', 'joining'))::int as pending,
           count(*) filter (where phase = 'failed'
               or (phase = 'ready' and (state <> 'online'
                   or last_seen_at < now() - make_interval(secs => @timeout_seconds::float8))))::int as degraded,
           coalesce(sum(hourly_micros) filter (where phase not in ('deleted', 'failed')), 0)::bigint as hourly_micros
    from counted
) hosts
cross join (
    select count(*)::int as workload_count
    from workloads wl join apps a on a.id = wl.app_id
    where a.workspace_id = @workspace_id and wl.desired_state = 'active' and a.state = 'active'
      and wl.active_release_id is not null
) served
where w.id = @workspace_id;

-- name: ComputeWorkloads :many
-- Deployed workloads with their active release's size and machine pin, by
-- app and name after the cursor.
select wl.id, a.name as app, wl.name, wl.kind, r.spec
from workloads wl
join apps a on a.id = wl.app_id
join releases r on r.id = wl.active_release_id
where a.workspace_id = @workspace_id and wl.desired_state <> 'deleted' and a.state <> 'deleted'
  and (a.name, wl.name) > (@after_app::text, @after_name::text)
order by a.name, wl.name
limit @max_rows;

-- name: ConnectionInstances :many
-- The cloud hosts of the account's connection, newest first after the
-- cursor; failed and deleted ones for a day.
select h.id, h.connection_id, h.region, h.availability_zone, h.instance_id, h.instance_type, h.market,
       h.phase, h.phase_message, h.phase_at, h.failure, h.state, h.last_seen_at, h.capacity_state,
       h.capacity_reason, h.gpu_type, h.gpu_count, h.cpu_millis, h.memory_bytes, h.launch_attempts,
       h.agent_version, h.launched_at, h.created_at
from hosts h
join cloud_connections cc on cc.id = h.connection_id
where cc.account_id = @account_id and h.kind = 'connection'
  and (h.phase not in ('deleted', 'failed') or h.phase_at > now() - interval '1 day')
  and h.id < @before_id
order by h.id desc
limit @max_rows;
