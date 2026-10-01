-- name: WorkloadInWorkspace :one
select w.id from workloads w join apps a on a.id = w.app_id
where w.id = @id and a.workspace_id = @workspace_id;

-- name: DeploymentPerformance :many
with t as (
    select date_bin(@bucket_width::interval, tk.created_at, to_timestamp(0)) as bucket,
           tk.status,
           case when tk.started_at is not null and tk.finished_at is not null
                then extract(epoch from tk.finished_at - tk.started_at) * 1000 end as run_ms
    from tasks tk
    where tk.workload_id = @workload_id
      and tk.id >= @from_id
      and tk.id < @to_id
      and tk.created_at >= @start_at and tk.created_at < @end_at
), task_buckets as (
    select bucket, count(run_ms)::int as finished,
           percentile_cont(0.5) within group (order by run_ms) as p50_ms,
           percentile_cont(0.95) within group (order by run_ms) as p95_ms,
           count(*) filter (where status = 'queued')::int as queued,
           count(*) filter (where status = 'running')::int as running,
           count(*) filter (where status = 'succeeded')::int as succeeded,
           count(*) filter (where status = 'failed')::int as failed,
           count(*) filter (where status = 'cancelled')::int as cancelled
    from t group by bucket
), cold as (
    select date_bin(@bucket_width::interval, c.created_at, to_timestamp(0)) as bucket,
           count(*)::int as cold_starts
    from releases r
    join containers c on c.release_id = r.id
    where r.workload_id = @workload_id
      and c.id >= @from_id
      and c.id < @to_id
      and c.created_at >= @start_at and c.created_at < @end_at
    group by 1
)
select coalesce(tb.bucket, cold.bucket)::timestamptz as bucket,
       coalesce(tb.finished, 0)::int as finished, tb.p50_ms, tb.p95_ms,
       coalesce(cold.cold_starts, 0)::int as cold_starts,
       coalesce(tb.queued, 0)::int as queued, coalesce(tb.running, 0)::int as running,
       coalesce(tb.succeeded, 0)::int as succeeded, coalesce(tb.failed, 0)::int as failed,
       coalesce(tb.cancelled, 0)::int as cancelled
from task_buckets tb
full join cold on cold.bucket = tb.bucket
order by 1;

-- name: TaskMetrics :one
with t as (
    select t.status,
           case when t.started_at is not null and t.finished_at is not null
                then extract(epoch from t.finished_at - t.started_at) * 1000 end as run_ms,
           case when t.started_at is not null
                then extract(epoch from t.started_at - t.created_at) * 1000 end as startup_ms
    from tasks t
    join workloads w on w.id = t.workload_id
    join apps a on a.id = w.app_id
    where t.workspace_id = @workspace_id
      and t.id >= @from_id
      and t.id < @to_id
      and t.created_at >= @start_at and t.created_at < @end_at
      and (sqlc.narg(app)::text is null or a.name = sqlc.narg(app)::text)
      and (sqlc.narg(function)::text is null or w.name = sqlc.narg(function)::text)
)
select count(*)::int as total,
       count(*) filter (where status = 'queued')::int as queued,
       count(*) filter (where status = 'running')::int as running,
       count(*) filter (where status = 'succeeded')::int as succeeded,
       count(*) filter (where status = 'failed')::int as failed,
       count(*) filter (where status = 'cancelled')::int as cancelled,
       count(run_ms)::int as finished,
       count(startup_ms)::int as started,
       coalesce(avg(run_ms), 0)::float8 as average_runtime_ms,
       coalesce(percentile_cont(0.5) within group (order by run_ms), 0)::float8 as runtime_p50,
       coalesce(percentile_cont(0.95) within group (order by run_ms), 0)::float8 as runtime_p95,
       coalesce(percentile_cont(0.99) within group (order by run_ms), 0)::float8 as runtime_p99,
       coalesce(percentile_cont(0.5) within group (order by startup_ms), 0)::float8 as startup_p50,
       coalesce(percentile_cont(0.95) within group (order by startup_ms), 0)::float8 as startup_p95
from t;

-- name: TaskActivity :many
-- Without an app, one series per app; with one, one per function.
select a.id as app_id, a.name as app_name,
       (case when sqlc.narg(app)::text is null then '' else w.name end)::text as function_name,
       date_bin(@bucket_width::interval, t.created_at, to_timestamp(0))::timestamptz as bucket,
       count(*) filter (where t.status = 'queued')::int as queued,
       count(*) filter (where t.status = 'running')::int as running,
       count(*) filter (where t.status = 'succeeded')::int as succeeded,
       count(*) filter (where t.status = 'failed')::int as failed,
       count(*) filter (where t.status = 'cancelled')::int as cancelled
from tasks t
join workloads w on w.id = t.workload_id
join apps a on a.id = w.app_id
where t.workspace_id = @workspace_id
  and t.id >= @from_id
  and t.id < @to_id
  and t.created_at >= @start_at and t.created_at < @end_at
  and (sqlc.narg(app)::text is null or a.name = sqlc.narg(app)::text)
group by 1, 2, 3, 4
order by 1, 3, 4;
