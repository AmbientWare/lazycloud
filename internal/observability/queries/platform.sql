-- name: PlatformTotals :one
select (select count(*) from users where status = 'active')::int as users,
       (select count(*) from workspaces)::int as workspaces,
       (select count(*) from apps where state <> 'deleted')::int as apps,
       (select count(*) from containers where state <> 'stopped')::int as live_containers,
       (select count(distinct user_id) from sign_ins where at >= @start_at and at < @end_at)::int as signed_in;

-- name: PlatformBuckets :many
-- Every workspace's activity per bucket. Tasks, requests, containers and
-- releases are read between the range's ids, so a range costs its own rows.
with buckets as (
    select d::timestamptz as bucket
    from generate_series(@start_at::timestamptz, sqlc.arg(end_at)::timestamptz - sqlc.arg(bucket_width)::interval,
                         @bucket_width::interval) d
), sign_ups as (
    select date_bin(@bucket_width::interval, u.created_at, to_timestamp(0)) as bucket, count(*)::int as n
    from users u where u.created_at >= @start_at and u.created_at < @end_at group by 1
), signed_in as (
    select date_bin(@bucket_width::interval, si.at, to_timestamp(0)) as bucket, count(distinct si.user_id)::int as n
    from sign_ins si where si.at >= @start_at and si.at < @end_at group by 1
), deployed as (
    select date_bin(@bucket_width::interval, r.created_at, to_timestamp(0)) as bucket, count(*)::int as n
    from releases r
    where r.id >= @from_id and r.id < @to_id and r.created_at >= @start_at and r.created_at < @end_at and r.version > 0
    group by 1
), task_work as (
    select date_bin(@bucket_width::interval, t.created_at, to_timestamp(0)) as bucket, t.workspace_id,
           count(*)::int as n, count(*) filter (where t.status = 'failed')::int as failed
    from tasks t
    where t.id >= @from_id and t.id < @to_id and t.created_at >= @start_at and t.created_at < @end_at
    group by 1, 2
), queued as (
    select date_bin(@bucket_width::interval, t.created_at, to_timestamp(0)) as bucket,
           percentile_cont(0.5) within group (order by extract(epoch from t.started_at - t.created_at) * 1000)::float8 as p50_ms,
           percentile_cont(0.95) within group (order by extract(epoch from t.started_at - t.created_at) * 1000)::float8 as p95_ms
    from tasks t
    where t.id >= @from_id and t.id < @to_id and t.created_at >= @start_at and t.created_at < @end_at
      and t.started_at is not null and t.attempt_count <= 1 and t.scheduled_for is null
    group by 1
), request_work as (
    select date_bin(@bucket_width::interval, h.started_at, to_timestamp(0)) as bucket, h.workspace_id,
           count(*)::int as n, count(*) filter (where h.status >= 500)::int as failed
    from http_requests h
    where h.id >= @from_id and h.id < @to_id and h.started_at >= @start_at and h.started_at < @end_at
    group by 1, 2
), work as (
    select bucket, count(distinct workspace_id)::int as workspaces,
           sum(tasks)::int as tasks, sum(failed_tasks)::int as failed_tasks,
           sum(requests)::int as requests, sum(failed_requests)::int as failed_requests
    from (
        select bucket, workspace_id, n as tasks, failed as failed_tasks, 0 as requests, 0 as failed_requests from task_work
        union all
        select bucket, workspace_id, 0, 0, n, failed from request_work
    ) w
    group by 1
), started as (
    select date_bin(@bucket_width::interval, c.created_at, to_timestamp(0)) as bucket, count(*)::int as n,
           count(*) filter (where c.state = 'stopped' and c.ready_at is null)::int as failed,
           percentile_cont(0.5) within group (order by extract(epoch from c.ready_at - c.created_at) * 1000)::float8 as p50_ms,
           percentile_cont(0.95) within group (order by extract(epoch from c.ready_at - c.created_at) * 1000)::float8 as p95_ms
    from containers c
    where c.id >= @from_id and c.id < @to_id and c.created_at >= @start_at and c.created_at < @end_at
    group by 1
)
select buckets.bucket,
       coalesce(sign_ups.n, 0)::int as sign_ups, coalesce(signed_in.n, 0)::int as sign_ins,
       coalesce(work.workspaces, 0)::int as active_workspaces, coalesce(deployed.n, 0)::int as deployments,
       coalesce(work.tasks, 0)::int as tasks, coalesce(work.failed_tasks, 0)::int as failed_tasks,
       coalesce(work.requests, 0)::int as requests, coalesce(work.failed_requests, 0)::int as failed_requests,
       coalesce(started.n, 0)::int as container_starts, coalesce(started.failed, 0)::int as failed_starts,
       started.p50_ms, started.p95_ms, queued.p50_ms as queue_p50_ms, queued.p95_ms as queue_p95_ms
from buckets
left join sign_ups on sign_ups.bucket = buckets.bucket
left join signed_in on signed_in.bucket = buckets.bucket
left join deployed on deployed.bucket = buckets.bucket
left join work on work.bucket = buckets.bucket
left join started on started.bucket = buckets.bucket
left join queued on queued.bucket = buckets.bucket
order by buckets.bucket;
