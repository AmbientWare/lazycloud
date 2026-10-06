-- name: InsertStartupStages :exec
-- Stages count only for a container assigned to the reporting host; the
-- first report of a stage wins, so restated reports change nothing. The
-- conversion stage, which the server stores from the container's
-- assignment, keeps its latest end, so the session that sends a start
-- extends what an earlier one stored as it ended.
insert into container_startup_stages (container_id, stage, started_at, finished_at, cached)
select c.id, s.stage, s.started_at, s.finished_at, s.cached
from (
    select unnest(@stages::text[]) as stage, unnest(@started_at::timestamptz[]) as started_at,
           unnest(@finished_at::timestamptz[]) as finished_at, unnest(@cached::bool[]) as cached
) s
join containers c on c.id = @container_id and c.host_id = @host_id
on conflict (container_id, stage) do update set finished_at = excluded.finished_at
where container_startup_stages.stage = 'conversion' and excluded.finished_at > container_startup_stages.finished_at;

-- name: ExtendConversionStage :exec
-- Moves the end of a stored conversion stage of a container assigned to the
-- host; a container with none keeps none.
update container_startup_stages s set finished_at = @finished_at
from containers c
where s.container_id = @container_id and s.stage = 'conversion' and c.id = s.container_id and c.host_id = @host_id
  and s.finished_at < @finished_at;

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
