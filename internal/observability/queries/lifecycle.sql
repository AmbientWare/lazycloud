-- name: InsertStartupStages :exec
-- Stages count only for a container assigned to the reporting host; the
-- first report of a stage wins, so restated reports change nothing.
insert into container_startup_stages (container_id, stage, started_at, finished_at, cached)
select c.id, s.stage, s.started_at, s.finished_at, s.cached
from (
    select unnest(@stages::text[]) as stage, unnest(@started_at::timestamptz[]) as started_at,
           unnest(@finished_at::timestamptz[]) as finished_at, unnest(@cached::bool[]) as cached
) s
join containers c on c.id = @container_id and c.host_id = @host_id
on conflict (container_id, stage) do nothing;

-- name: ContainerLifecycles :many
select c.id, a.name as app_name, w.name as function_name, c.state, c.stop_reason, c.exit_message,
       h.name as host_name, c.created_at, c.assigned_at, c.ready_at, c.drain_started_at, c.stopped_at,
       now()::timestamptz as observed_at
from containers c
join releases r on r.id = c.release_id
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
left join hosts h on h.id = c.host_id
where c.workspace_id = @workspace_id and c.id = any(@ids::uuid[])
order by c.id;

-- name: StartupStages :many
select container_id, stage, started_at, finished_at, cached
from container_startup_stages
where container_id = any(@ids::uuid[])
order by container_id, started_at;
