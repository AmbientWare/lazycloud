-- Cost queries read only the ledger, through ledger_entries_payer, and place
-- each entry by when its interval started.

-- name: CostRows :many
-- One page of cost groups, most expensive first, then by key. Groups are
-- the workspace, app and category, and by_workload splits app rows by
-- workload and an app's artifact storage into its own row. Each disk is one
-- group. Components sum in the same pass.
select g.workspace_id, g.app_key::uuid as app_key, g.workload_key::uuid as workload_key, g.category::text as category, g.disk_key::uuid as disk_key,
       sum(g.cost_nanos)::bigint as cost_nanos,
       sum(g.container_nanos)::bigint as container_nanos,
       sum(g.cpu_nanos)::bigint as cpu_nanos,
       sum(g.memory_nanos)::bigint as memory_nanos,
       sum(g.gpu_nanos)::bigint as gpu_nanos,
       sum(g.volume_nanos)::bigint as volume_nanos,
       sum(g.disk_nanos)::bigint as disk_nanos,
       sum(g.seconds)::float8 as seconds,
       sum(g.core_seconds)::float8 as core_seconds,
       sum(g.gib_seconds)::float8 as gib_seconds,
       sum(g.card_seconds)::float8 as card_seconds,
       sum(g.volume_gib_seconds)::float8 as volume_gib_seconds,
       sum(g.disk_gib_seconds)::float8 as disk_gib_seconds
from (
    select e.workspace_id,
           coalesce(e.app_id, '00000000-0000-0000-0000-000000000000'::uuid) as app_key,
           (case when @by_workload::bool then coalesce(e.workload_id, '00000000-0000-0000-0000-000000000000'::uuid)
                 else '00000000-0000-0000-0000-000000000000'::uuid end) as workload_key,
           (case when e.category in ('image-build', 'disk') then e.category
                 when e.category = 'artifacts' and @by_workload::bool then 'artifacts'
                 else '' end)::text as category,
           (case when e.category = 'disk' then e.source_id
                 else '00000000-0000-0000-0000-000000000000'::uuid end) as disk_key,
           e.cost_nanos, e.container_nanos, e.cpu_nanos, e.memory_nanos, e.gpu_nanos,
           (case when e.source_kind in ('volume', 'artifacts') then e.storage_nanos else 0 end) as volume_nanos,
           (case when e.source_kind = 'disk' then e.storage_nanos + e.attached_nanos else 0 end) as disk_nanos,
           (case when e.source_kind = 'container' then extract(epoch from e.ended_at - e.started_at) else 0 end) as seconds,
           e.cpu_millis / 1000.0 * extract(epoch from e.ended_at - e.started_at) as core_seconds,
           e.memory_bytes / 1073741824.0 * extract(epoch from e.ended_at - e.started_at) as gib_seconds,
           e.gpu_count * extract(epoch from e.ended_at - e.started_at) as card_seconds,
           (case when e.source_kind in ('volume', 'artifacts')
                 then e.stored_bytes / 1073741824.0 * extract(epoch from e.ended_at - e.started_at) else 0 end) as volume_gib_seconds,
           (case when e.source_kind = 'disk'
                 then e.stored_bytes / 1073741824.0 * extract(epoch from e.ended_at - e.started_at) else 0 end) as disk_gib_seconds
    from ledger_entries e
    where e.user_id = @user_id and e.started_at >= @start_at and e.started_at < @end_at
      and (sqlc.narg(workspace_id)::uuid is null or e.workspace_id = sqlc.narg(workspace_id)::uuid)
      and (sqlc.narg(app_id)::uuid is null or e.app_id = sqlc.narg(app_id)::uuid)
      and (@category::text = ''
           or (@category::text = 'image-build' and e.category = 'image-build')
           or (@category::text = 'disk' and e.category = 'disk')
           or (@category::text = 'unattributed' and e.app_id is null and coalesce(e.category, '') not in ('image-build', 'disk')))
) g
group by 1, 2, 3, 4, 5
having not @has_cursor::bool
    or sum(g.cost_nanos) < @after_cost::bigint
    or (sum(g.cost_nanos) = @after_cost::bigint
        and (g.workspace_id, g.app_key, g.workload_key, g.category, g.disk_key)
            > (@after_workspace::uuid, @after_app::uuid, @after_workload::uuid, @after_category::text, @after_disk::uuid))
order by 6 desc, 1, 2, 3, 4, 5
limit @row_limit;

-- name: WindowCost :one
select coalesce(sum(e.cost_nanos), 0)::bigint
from ledger_entries e
where e.user_id = @user_id and e.started_at >= @start_at and e.started_at < @end_at
  and (sqlc.narg(workspace_id)::uuid is null or e.workspace_id = sqlc.narg(workspace_id)::uuid)
  and (sqlc.narg(app_id)::uuid is null or e.app_id = sqlc.narg(app_id)::uuid)
  and (@category::text = ''
       or (@category::text = 'image-build' and e.category = 'image-build')
       or (@category::text = 'disk' and e.category = 'disk')
       or (@category::text = 'unattributed' and e.app_id is null and coalesce(e.category, '') not in ('image-build', 'disk')));

-- name: CostBuckets :many
-- Cost per whole bucket from the window's start and invoice line; only
-- buckets with cost.
select floor(extract(epoch from e.started_at - @start_at::timestamptz) / @width_seconds::float8)::int as bucket,
       (case e.source_kind when 'container' then 'compute_runtime' when 'disk' then 'disk'
             else 'volume_storage' end)::text as dimension,
       sum(e.cost_nanos)::bigint as cost_nanos
from ledger_entries e
where e.user_id = @user_id and e.started_at >= @start_at::timestamptz and e.started_at < @end_at::timestamptz
group by 1, 2
order by 1, 2;

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

-- name: DiskNames :many
select id, name from disks where id = any(@ids::uuid[]) and state = 'active';
