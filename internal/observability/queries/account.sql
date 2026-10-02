-- name: LiveContainerCounts :many
-- From the containers_live_workspace partial index of each workspace.
select c.workspace_id, c.state, count(*)::int as containers
from containers c
where c.workspace_id = any(@workspace_ids::uuid[]) and c.state <> 'stopped'
group by 1, 2;

-- name: ContainerStarts :many
-- Containers created per workspace, app and bucket. Build containers have
-- no app.
select c.workspace_id, a.id as app_id, a.name as app_name,
       date_bin(@bucket_width::interval, c.created_at, to_timestamp(0))::timestamptz as bucket,
       count(*)::float8 as value
from containers c
left join releases r on r.id = c.release_id
left join workloads w on w.id = r.workload_id
left join apps a on a.id = w.app_id
where c.workspace_id = any(@workspace_ids::uuid[])
  and c.id >= @from_id
  and c.id < @to_id
  and c.created_at >= @start_at and c.created_at < @end_at
group by 1, 2, 3, 4;

-- name: TaskStarts :many
select t.workspace_id, a.id as app_id, a.name as app_name,
       date_bin(@bucket_width::interval, t.created_at, to_timestamp(0))::timestamptz as bucket,
       count(*)::float8 as value
from tasks t
join workloads w on w.id = t.workload_id
join apps a on a.id = w.app_id
where t.workspace_id = any(@workspace_ids::uuid[])
  and t.id >= @from_id
  and t.id < @to_id
  and t.created_at >= @start_at and t.created_at < @end_at
group by 1, 2, 3, 4;

-- name: Allocations :many
-- Reserved amount-seconds per workspace, app and bucket, where the amount
-- weighs CPU, memory and GPU reservations; a container reserves the GPUs
-- its release names. A container reserves capacity on its
-- host from assignment until it stops. The live partial index and the
-- stopped index together find every container alive in the range without
-- reading older history.
with alive as (
    select c.workspace_id, c.release_id, c.assigned_at, c.stopped_at,
           (c.cpu_millis * sqlc.arg(per_cpu_milli)::float8 + c.memory_bytes * sqlc.arg(per_memory_byte)::float8)::float8 as amount
    from containers c
    where c.workspace_id = any(@workspace_ids::uuid[]) and c.state <> 'stopped' and c.assigned_at < @end_at::timestamptz
    union all
    select c.workspace_id, c.release_id, c.assigned_at, c.stopped_at,
           (c.cpu_millis * sqlc.arg(per_cpu_milli)::float8 + c.memory_bytes * sqlc.arg(per_memory_byte)::float8)::float8 as amount
    from containers c
    where c.workspace_id = any(@workspace_ids::uuid[]) and c.state = 'stopped'
      and c.stopped_at > @start_at::timestamptz and c.assigned_at < @end_at::timestamptz
), buckets as (
    select b as bucket
    from generate_series(@start_at::timestamptz, sqlc.arg(end_at)::timestamptz - sqlc.arg(bucket_width)::interval,
                         @bucket_width::interval) as b
)
select alive.workspace_id, a.id as app_id, a.name as app_name, buckets.bucket::timestamptz as bucket,
       sum(extract(epoch from
               least(coalesce(alive.stopped_at, now()), buckets.bucket + @bucket_width::interval)
               - greatest(alive.assigned_at, buckets.bucket))
           * (alive.amount + coalesce(release_gpus(r.spec), 0) * sqlc.arg(per_gpu)::float8))::float8 as value
from alive
join buckets on alive.assigned_at < buckets.bucket + @bucket_width::interval
            and coalesce(alive.stopped_at, now()) > buckets.bucket
left join releases r on r.id = alive.release_id
left join workloads w on w.id = r.workload_id
left join apps a on a.id = w.app_id
group by 1, 2, 3, 4;
