-- Cost queries read only the ledger, through ledger_entries_payer, and place
-- each entry by when its interval started.

-- name: CostRows :many
-- One page of cost groups, most expensive first, then by key. by_workload
-- groups app rows further by workload. Components sum in the same pass.
select e.workspace_id,
       coalesce(e.app_id, '00000000-0000-0000-0000-000000000000'::uuid)::uuid as app_key,
       (case when @by_workload::bool then coalesce(e.workload_id, '00000000-0000-0000-0000-000000000000'::uuid)
             else '00000000-0000-0000-0000-000000000000'::uuid end)::uuid as workload_key,
       coalesce(e.category, '')::text as category,
       sum(e.cost_nanos)::bigint as cost_nanos,
       sum(e.container_nanos)::bigint as container_nanos,
       sum(e.cpu_nanos)::bigint as cpu_nanos,
       sum(e.memory_nanos)::bigint as memory_nanos,
       sum(e.gpu_nanos)::bigint as gpu_nanos,
       sum(extract(epoch from e.ended_at - e.started_at))::float8 as seconds,
       sum(e.cpu_millis / 1000.0 * extract(epoch from e.ended_at - e.started_at))::float8 as core_seconds,
       sum(e.memory_bytes / 1073741824.0 * extract(epoch from e.ended_at - e.started_at))::float8 as gib_seconds,
       sum(e.gpu_count * extract(epoch from e.ended_at - e.started_at))::float8 as card_seconds
from ledger_entries e
where e.user_id = @user_id and e.started_at >= @start_at and e.started_at < @end_at
  and (sqlc.narg(workspace_id)::uuid is null or e.workspace_id = sqlc.narg(workspace_id)::uuid)
  and (sqlc.narg(app_id)::uuid is null or e.app_id = sqlc.narg(app_id)::uuid)
  and (@category::text = ''
       or (@category::text = 'image-build' and e.category = 'image-build')
       or (@category::text = 'unattributed' and e.app_id is null and e.category is null))
group by 1, 2, 3, 4
having not @has_cursor::bool
    or sum(e.cost_nanos) < @after_cost::bigint
    or (sum(e.cost_nanos) = @after_cost::bigint
        and (e.workspace_id, coalesce(e.app_id, '00000000-0000-0000-0000-000000000000'::uuid),
             (case when @by_workload::bool then coalesce(e.workload_id, '00000000-0000-0000-0000-000000000000'::uuid)
                   else '00000000-0000-0000-0000-000000000000'::uuid end),
             coalesce(e.category, ''))
            > (@after_workspace::uuid, @after_app::uuid, @after_workload::uuid, @after_category::text))
order by 5 desc, 1, 2, 3, 4
limit @row_limit;

-- name: WindowCost :one
select coalesce(sum(e.cost_nanos), 0)::bigint
from ledger_entries e
where e.user_id = @user_id and e.started_at >= @start_at and e.started_at < @end_at
  and (sqlc.narg(workspace_id)::uuid is null or e.workspace_id = sqlc.narg(workspace_id)::uuid)
  and (sqlc.narg(app_id)::uuid is null or e.app_id = sqlc.narg(app_id)::uuid)
  and (@category::text = ''
       or (@category::text = 'image-build' and e.category = 'image-build')
       or (@category::text = 'unattributed' and e.app_id is null and e.category is null));

-- name: CostBuckets :many
-- Cost per whole bucket from the window's start; only buckets with cost.
select floor(extract(epoch from e.started_at - @start_at::timestamptz) / @width_seconds::float8)::int as bucket,
       sum(e.cost_nanos)::bigint as cost_nanos
from ledger_entries e
where e.user_id = @user_id and e.started_at >= @start_at::timestamptz and e.started_at < @end_at::timestamptz
group by 1
order by 1;

-- name: SubscriptionCovered :one
select coalesce(sum(subscription_nanos), 0)::bigint
from billing_hours
where user_id = @user_id and hour >= @start_at::timestamptz and hour < @end_at::timestamptz;

-- name: WorkspaceNames :many
select id, name from workspaces where id = any(@ids::uuid[]);

-- name: AppNames :many
select id, name, state from apps where id = any(@ids::uuid[]);

-- name: WorkloadNames :many
select id, name, kind from workloads where id = any(@ids::uuid[]);
