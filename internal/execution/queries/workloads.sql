-- name: InstanceRelease :one
-- The release an instance runs, with its workload, in the workspace.
select r.id, r.workload_id, w.kind, w.name, a.name as app_name, a.workspace_id, r.version, r.spec,
       (w.desired_state <> 'deleted' and a.state = 'active' and ws.state = 'active')::bool as live
from releases r
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
where r.id = @release_id and a.workspace_id = @workspace_id;

-- name: InsertInstance :one
insert into containers (
    workspace_id, release_id, state, slots, cpu_millis, memory_bytes, gpu_count, rate_class,
    purpose, keep_warm_seconds, command, snapshot_id, block_network, allow_list, exposed_ports, created_by_container
)
values (
    @workspace_id, @release_id, 'pending', 1, @cpu_millis, @memory_bytes, @gpu_count, @rate_class,
    @purpose, sqlc.narg('keep_warm_seconds'), sqlc.narg('command')::text[], sqlc.narg('snapshot_id'),
    @block_network, @allow_list::text[], @exposed_ports::int[], sqlc.narg('created_by_container')
)
returning id, created_at;

-- name: InstanceView :one
-- A container as an instance: its release, workload and lifetime.
select c.id, c.release_id::uuid as release_id, a.name as app_name, w.name as workload_name, w.kind, c.state,
       c.purpose, c.keep_warm_seconds, c.active_until, c.stop_reason, c.exit_message, c.exit_code,
       c.created_at, c.ready_at, c.host_id, c.exposed_ports, r.spec
from containers c
join releases r on r.id = c.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
where c.id = @id and c.workspace_id = @workspace_id;

-- name: UpsertContainerLeases :exec
-- Holder keeps the containers active for ttl_seconds more.
insert into container_leases (container_id, holder_id, expires_at)
select unnest(@container_ids::uuid[]), @holder_id, now() + make_interval(secs => @ttl_seconds::float8)
on conflict (container_id, holder_id) do update set expires_at = excluded.expires_at;

-- name: EndContainerLeases :exec
-- The connections ended now. The rows stay so a container stays active for
-- its keep-warm window after the last one; the sweep deletes them later.
update container_leases set expires_at = least(expires_at, now())
where holder_id = @holder_id and container_id = any(@container_ids::uuid[]);

-- name: DeleteOldContainerLeases :exec
-- Leases of stopped containers, and leases past any keep-warm window.
delete from container_leases l
using containers c
where c.id = l.container_id
  and (c.state = 'stopped' or l.expires_at < now() - interval '8 days');

-- name: TouchContainer :exec
-- A call on the container is activity: it stays active for its keep-warm
-- window. Writes only when that moves active_until by a meaningful amount.
update containers
set active_until = now() + make_interval(secs => keep_warm_seconds)
where id = @id and keep_warm_seconds is not null and state <> 'stopped'
  and (active_until is null or active_until < now() + make_interval(secs => keep_warm_seconds / 2.0));

-- name: SetContainerTTL :one
-- ttl seconds from now become the instance's idle window; null never idles.
update containers
set keep_warm_seconds = sqlc.narg('keep_warm_seconds'),
    active_until = case when sqlc.narg('keep_warm_seconds')::int is null then null
                        else now() + make_interval(secs => sqlc.narg('keep_warm_seconds')::int) end
where id = @id and workspace_id = @workspace_id and state in ('pending', 'starting', 'ready')
returning keep_warm_seconds, active_until;

-- name: DrainIdleInstances :many
-- Ready instances and shell containers past their lifetime with no
-- connection inside its window drain; their hosts stop them.
update containers c
set state = 'draining', drain_started_at = now()
where c.id in (
    select i.id from containers i
    where i.purpose <> 'serve' and i.state = 'ready'
      and i.keep_warm_seconds is not null and i.active_until < now()
      and not exists (
          select 1 from container_leases l
          where l.container_id = i.id and l.expires_at + make_interval(secs => i.keep_warm_seconds) > now()
      )
    order by i.active_until, i.id
    limit @batch_size
    for update skip locked
)
  and c.state = 'ready'
returning c.id, c.host_id;

-- name: PodReleases :many
-- Pod releases after @after_id that need a decision: live serve
-- containers, a wake or a count, or an always-on active release. Each source
-- reads live rows only.
with candidates as (
    select c.release_id
    from containers c
    join releases r on r.id = c.release_id
    join workloads w on w.id = r.workload_id
    where c.state <> 'stopped' and c.purpose = 'serve' and w.kind = 'pod' and c.release_id > @after_id::uuid
    union
    select w.active_release_id
    from pod_states s
    join workloads w on w.id = s.workload_id
    where not s.parked and (s.replicas > 0 or s.woken_at > now() - interval '15 minutes')
      and w.active_release_id > @after_id
    union
    select w.active_release_id
    from workloads w
    join apps a on a.id = w.app_id
    join releases r on r.id = w.active_release_id
    where w.kind = 'pod' and w.desired_state = 'active' and a.state = 'active'
      and w.active_release_id > @after_id
      and coalesce((r.spec ->> 'keep_warm_seconds')::int, 0) = -1
),
batch as (
    select release_id from candidates order by release_id limit @batch_size
)
select r.id as release_id,
       a.workspace_id,
       w.id as workload_id,
       (w.active_release_id is not distinct from r.id and w.desired_state = 'active' and a.state = 'active'
        and ws.state = 'active')::bool as active,
       (w.desired_state <> 'active' or a.state <> 'active' or ws.state <> 'active')::bool as stopping,
       (r.start_failures >= @start_failure_limit::int)::bool as failed,
       s.replicas,
       coalesce(s.woken_at > now() - interval '15 minutes', false)::bool as woken,
       coalesce(s.parked, false)::bool as parked,
       (coalesce((r.spec ->> 'keep_warm_seconds')::int, 0) = -1)::bool as always_on,
       coalesce(nullif((r.spec ->> 'keep_warm_seconds')::int, -1), 600)::int as keep_warm_seconds,
       (jsonb_array_length(coalesce(r.spec -> 'disks', '[]'::jsonb)) > 0)::bool as has_disks,
       coalesce((r.spec -> 'autoscaler' ->> 'max_containers')::int, 1)::int as max_containers,
       (r.spec -> 'resources' ->> 'cpu_millis')::bigint as cpu_millis,
       ((r.spec -> 'resources' ->> 'memory_mib')::bigint * 1048576)::bigint as memory_bytes,
       greatest(coalesce((r.spec -> 'resources' ->> 'gpu_count')::int, 0),
                case when jsonb_array_length(coalesce(r.spec -> 'resources' -> 'gpu', '[]'::jsonb)) > 0 then 1 else 0 end)::int
           as gpu_count,
       coalesce(array(select jsonb_array_elements_text(r.spec -> 'resources' -> 'gpu')), '{}')::text[] as gpu_models,
       coalesce((r.spec -> 'placement' ->> 'preemptible')::boolean, true)::bool as preemptible,
       (coalesce(r.spec -> 'placement' ->> 'region', '') <> ''
        or coalesce(r.spec -> 'placement' ->> 'availability_zone', '') <> '')::bool as pinned,
       coalesce((r.spec -> 'pod' ->> 'block_network')::boolean, false)::bool as block_network,
       coalesce(array(select jsonb_array_elements_text(r.spec -> 'pod' -> 'allow_list')), '{}')::text[] as allow_list,
       exists (
           select 1 from containers ac
           where ac.release_id = w.active_release_id and ac.state = 'ready' and ac.purpose = 'serve'
       )::bool as active_ready,
       c.pending::int as pending,
       c.starting::int as starting,
       c.ready::int as ready,
       c.draining::int as draining,
       c.warm::int as warm
from batch
join releases r on r.id = batch.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
left join pod_states s on s.workload_id = w.id
cross join lateral (
    select count(*) filter (where c.state = 'pending') as pending,
           count(*) filter (where c.state = 'starting') as starting,
           count(*) filter (where c.state = 'ready') as ready,
           count(*) filter (where c.state = 'draining') as draining,
           -- Starting containers and ready ones still inside their
           -- keep-warm window or holding a connection.
           count(*) filter (where c.state = 'starting' or (c.state = 'ready' and (
               c.keep_warm_seconds is null or c.active_until is null or c.active_until > now()
               or exists (
                   select 1 from container_leases l
                   where l.container_id = c.id and l.expires_at + make_interval(secs => c.keep_warm_seconds) > now()
               )))) as warm
    from containers c where c.release_id = r.id and c.state <> 'stopped' and c.purpose = 'serve'
) c
order by r.id;

-- name: CreatePendingPodContainers :many
insert into containers (
    workspace_id, release_id, state, slots, cpu_millis, memory_bytes, gpu_count, rate_class,
    keep_warm_seconds, block_network, allow_list
)
select @workspace_id, @release_id::uuid, 'pending', 1, @cpu_millis, @memory_bytes, @gpu_count, @rate_class,
       sqlc.narg('keep_warm_seconds'), @block_network, @allow_list::text[]
from generate_series(1, @count::int)
returning id;

-- name: DrainPodContainers :many
-- Idle ready containers first, then the newest; the oldest active ones are
-- the ones holding connections.
update containers
set state = 'draining', drain_started_at = now()
where id in (
    select d.id from containers d
    where d.release_id = @release_id::uuid and d.purpose = 'serve' and d.state in ('starting', 'ready')
    order by (d.state = 'ready' and d.keep_warm_seconds is not null and d.active_until < now()
              and not exists (
                  select 1 from container_leases l
                  where l.container_id = d.id and l.expires_at + make_interval(secs => d.keep_warm_seconds) > now()
              )) desc,
             d.created_at desc, d.id desc
    limit @count
    for update skip locked
)
  and state in ('starting', 'ready')
returning id, host_id;

-- name: LockPodWorkload :one
-- A pod in the workspace with its app, locked so a scale, wake or park is
-- decided against its current state.
select w.id, w.kind, w.name, w.desired_state, a.state as app_state, w.active_release_id, r.spec
from workloads w
join apps a on a.id = w.app_id
left join releases r on r.id = w.active_release_id
where w.id = @id and a.workspace_id = @workspace_id
for update of w;

-- name: SetPodReplicas :exec
insert into pod_states (workload_id, replicas) values (@workload_id, @replicas)
on conflict (workload_id) do update set replicas = excluded.replicas;

-- name: WakePod :exec
-- A connection or Start asks for a container now.
insert into pod_states (workload_id, woken_at, parked) values (@workload_id, now(), false)
on conflict (workload_id) do update set woken_at = now(), parked = false;

-- name: ParkPod :exec
insert into pod_states (workload_id, woken_at, parked) values (@workload_id, null, true)
on conflict (workload_id) do update set woken_at = null, parked = true;

-- name: PodState :one
select replicas, woken_at, parked from pod_states where workload_id = @workload_id;

-- name: DrainServeContainersOfWorkload :many
update containers c
set state = 'draining', drain_started_at = now()
from releases r
where r.id = c.release_id and r.workload_id = @workload_id
  and c.purpose = 'serve' and c.state in ('starting', 'ready')
returning c.id, c.host_id;

-- name: StopPendingServeContainersOfWorkload :many
update containers c
set state = 'stopped', stop_reason = 'stopped', exit_message = 'stopped by request', stopped_at = now()
from releases r
where r.id = c.release_id and r.workload_id = @workload_id
  and c.purpose = 'serve' and c.state = 'pending'
returning c.id;

-- name: ReadyPodContainers :many
-- Ready serve containers of a pod's releases with their hosts, newest
-- version first and then the oldest container.
select c.id, c.host_id, c.release_id::uuid as release_id
from containers c
join releases r on r.id = c.release_id
where r.workload_id = @workload_id and c.purpose = 'serve' and c.state = 'ready' and c.host_id is not null
order by r.version desc nulls last, c.ready_at, c.id;

-- name: ContainerRoute :one
-- A live container with what routing to it needs.
select c.id, c.host_id, c.state, c.purpose, c.exposed_ports, c.workspace_id, ws.name as workspace_name,
       w.id as workload_id, w.kind, a.state as app_state, w.desired_state, r.spec, c.created_by_container
from containers c
join releases r on r.id = c.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = c.workspace_id
where c.id = @id;

-- name: PodRelease :one
-- A pod or sandbox release with what routing to it needs.
select r.id, r.workload_id, w.kind, a.workspace_id, ws.name as workspace_name, r.spec,
       (w.desired_state = 'active' and a.state = 'active')::bool as accepting
from releases r
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
where r.id = @id and w.kind in ('pod', 'sandbox');

-- name: SetContainerNetwork :one
update containers
set block_network = @block_network, allow_list = @allow_list::text[], network_version = network_version + 1
where id = @id and workspace_id = @workspace_id and state <> 'stopped'
returning host_id, block_network, allow_list, network_version;

-- name: ContainerNetworkApplied :one
select state, network_applied_version, network_error from containers where id = @id;

-- name: RecordNetworkApplied :one
-- Keeps the newest version a host reports for a container it holds.
update containers set network_applied_version = @version, network_error = sqlc.narg('error')
where id = @id and host_id = @host_id and network_applied_version < @version
returning id;

-- name: ContainerNetwork :one
select block_network, allow_list from containers where id = @id and workspace_id = @workspace_id;

-- name: NetworkPoliciesOnHost :many
-- Live containers on the host whose policy changed after their start, which
-- carried the first one.
select id, block_network, allow_list, network_version from containers
where host_id = @host_id and state in ('starting', 'ready', 'draining') and network_version > 0
order by id;

-- name: ExposePort :one
update containers
set exposed_ports = case when @port::int = any(exposed_ports) then exposed_ports else array_append(exposed_ports, @port::int) end
where id = @id and workspace_id = @workspace_id and state <> 'stopped'
returning exposed_ports;

-- name: ListSandboxes :many
-- Sandbox containers newest first below the cursor.
select c.id, c.release_id::uuid as release_id, a.name as app_name, w.name as workload_name, c.state, c.stop_reason,
       c.created_at, c.ready_at, c.stopped_at, r.spec
from containers c
join releases r on r.id = c.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
where c.workspace_id = @workspace_id and c.purpose = 'instance' and w.kind = 'sandbox' and c.id < @before
  and (sqlc.narg('app')::text is null or a.name = sqlc.narg('app'))
  and (sqlc.narg('search')::text is null
       or strpos(lower(w.name), sqlc.narg('search')::text) > 0
       or strpos(a.name, sqlc.narg('search')::text) > 0
       or strpos(c.id::text, sqlc.narg('search')::text) > 0)
order by c.id desc
limit @max_rows;

-- name: SandboxStats :one
select count(*) filter (where c.state in ('pending', 'starting', 'ready', 'draining'))::int as concurrent,
       count(*)::int as total_created,
       count(*) filter (where c.created_at > now() - interval '24 hours')::int as created_day,
       count(*) filter (where c.state in ('pending', 'starting'))::int as pending,
       count(*) filter (where c.state = 'ready')::int as running,
       count(*) filter (where c.state = 'draining')::int as stopping,
       count(*) filter (where c.state = 'stopped' and c.stop_reason in ('stopped', 'exited'))::int as stopped,
       count(*) filter (where c.state = 'stopped' and c.stop_reason not in ('stopped', 'exited'))::int as failed
from containers c
join releases r on r.id = c.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
where c.workspace_id = @workspace_id and c.purpose = 'instance' and w.kind = 'sandbox'
  and (sqlc.narg('app')::text is null or a.name = sqlc.narg('app'));

-- name: SandboxCreatedDays :many
select (date_trunc('day', c.created_at at time zone 'UTC') at time zone 'UTC')::timestamptz as day, count(*)::int as created
from containers c
join releases r on r.id = c.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
where c.workspace_id = @workspace_id and c.purpose = 'instance' and w.kind = 'sandbox' and c.created_at > now() - interval '30 days'
  and (sqlc.narg('app')::text is null or a.name = sqlc.narg('app'))
group by 1
order by 1;

-- name: ContainerReleaseOf :one
select release_id::uuid as release_id from containers where id = @id and release_id is not null;

-- name: StopUnservedPendingInstances :many
-- Pending instances and shell containers whose workload stopped, app
-- paused or workspace is being deleted never start.
update containers c
set state = 'stopped', stop_reason = 'stopped', exit_message = 'the workload stopped', stopped_at = now()
from releases r
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
where r.id = c.release_id and c.purpose <> 'serve' and c.state = 'pending'
  and (w.desired_state <> 'active' or a.state <> 'active' or ws.state <> 'active')
returning c.id;

-- name: DrainUnservedInstances :many
-- Running instances and shell containers of such workloads drain; their
-- hosts stop them.
update containers c
set state = 'draining', drain_started_at = now()
from releases r
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
join workspaces ws on ws.id = a.workspace_id
where r.id = c.release_id and c.purpose <> 'serve' and c.state in ('starting', 'ready')
  and (w.desired_state <> 'active' or a.state <> 'active' or ws.state <> 'active')
returning c.id, c.host_id;
