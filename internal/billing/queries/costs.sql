-- Cost queries read only the ledger, through ledger_entries_payer, and place
-- each entry by when its interval started.

-- name: CostRows :many
-- One page of cost groups, most expensive first, then by key. Groups are
-- the workspace, app and category, and by_workload splits app rows by
-- workload and an app's artifact storage into its own row. Each disk is one
-- group. by_task also splits a container's cost among the attempts that ran
-- in it, each by the time it overlapped an entry; time no attempt used
-- (starting, keeping warm) stays with the workload, and attempts running at
-- once share by their overlap. Components sum in the same pass.
with entries as (
    select e.*
    from ledger_entries e
    where e.user_id = @user_id and e.started_at >= @start_at and e.started_at < @end_at
      and (sqlc.narg(workspace_id)::uuid is null or e.workspace_id = sqlc.narg(workspace_id)::uuid)
      and (sqlc.narg(app_id)::uuid is null or e.app_id = sqlc.narg(app_id)::uuid)
      and (@category::text = ''
           or (@category::text = 'image-build' and e.category = 'image-build')
           or (@category::text = 'disk' and e.category = 'disk')
           or (@category::text = 'unattributed' and e.app_id is null and coalesce(e.category, '') not in ('image-build', 'disk')))
), attempt_spans as (
    select e.id, a.task_id,
           extract(epoch from least(coalesce(a.finished_at, e.ended_at), e.ended_at) - greatest(a.started_at, e.started_at))::float8 as seconds
    from entries e
    join attempts a on a.container_id = e.source_id
         and a.started_at < e.ended_at and (a.finished_at is null or a.finished_at > e.started_at)
    where @by_task::bool and e.source_kind = 'container' and e.category is null
), runs as (
    select o.id, o.task_id,
           sum(o.seconds) / greatest(max(extract(epoch from e.ended_at - e.started_at))::float8, max(t.seconds)) as share
    from attempt_spans o
    join entries e on e.id = o.id
    join (select id, sum(seconds) as seconds from attempt_spans group by id) t on t.id = o.id
    group by o.id, o.task_id
), parts as (
    select r.id, r.task_id, r.share from runs r
    union all
    select e.id, null::uuid, 1 - coalesce(r.share, 0)
    from entries e
    left join (select id, sum(share) as share from runs group by id) r on r.id = e.id
)
select g.workspace_id, g.app_key::uuid as app_key, g.workload_key::uuid as workload_key, g.task_key::uuid as task_key,
       g.category::text as category, g.disk_key::uuid as disk_key,
       round(sum(g.cost_nanos))::bigint as cost_nanos,
       round(sum(g.container_nanos))::bigint as container_nanos,
       round(sum(g.cpu_nanos))::bigint as cpu_nanos,
       round(sum(g.memory_nanos))::bigint as memory_nanos,
       round(sum(g.gpu_nanos))::bigint as gpu_nanos,
       round(sum(g.volume_nanos))::bigint as volume_nanos,
       round(sum(g.disk_nanos))::bigint as disk_nanos,
       round(sum(g.egress_nanos))::bigint as egress_nanos,
       sum(g.egress_gib)::float8 as egress_gib,
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
           coalesce(p.task_id, '00000000-0000-0000-0000-000000000000'::uuid) as task_key,
           (case when e.category in ('image-build', 'disk') then e.category
                 when e.category = 'artifacts' and @by_workload::bool then 'artifacts'
                 else '' end)::text as category,
           (case when e.category = 'disk' then e.source_id
                 else '00000000-0000-0000-0000-000000000000'::uuid end) as disk_key,
           e.cost_nanos * p.share as cost_nanos, e.container_nanos * p.share as container_nanos,
           e.cpu_nanos * p.share as cpu_nanos, e.memory_nanos * p.share as memory_nanos, e.gpu_nanos * p.share as gpu_nanos,
           (case when e.source_kind in ('volume', 'artifacts') then e.storage_nanos else 0 end) * p.share as volume_nanos,
           (case when e.source_kind = 'disk' then e.storage_nanos + e.attached_nanos else 0 end) * p.share as disk_nanos,
           e.egress_nanos * p.share as egress_nanos, e.egress_bytes / 1073741824.0 * p.share as egress_gib,
           (case when e.source_kind = 'container' then extract(epoch from e.ended_at - e.started_at) else 0 end) * p.share as seconds,
           e.cpu_millis / 1000.0 * extract(epoch from e.ended_at - e.started_at) * p.share as core_seconds,
           e.memory_bytes / 1073741824.0 * extract(epoch from e.ended_at - e.started_at) * p.share as gib_seconds,
           e.gpu_count * extract(epoch from e.ended_at - e.started_at) * p.share as card_seconds,
           (case when e.source_kind in ('volume', 'artifacts')
                 then e.stored_bytes / 1073741824.0 * extract(epoch from e.ended_at - e.started_at) else 0 end) * p.share as volume_gib_seconds,
           (case when e.source_kind = 'disk'
                 then e.stored_bytes / 1073741824.0 * extract(epoch from e.ended_at - e.started_at) else 0 end) * p.share as disk_gib_seconds
    from parts p
    join entries e on e.id = p.id
    where p.share > 1e-9
) g
group by 1, 2, 3, 4, 5, 6
having not @has_cursor::bool
    or round(sum(g.cost_nanos)) < @after_cost::bigint
    or (round(sum(g.cost_nanos)) = @after_cost::bigint
        and (g.workspace_id, g.app_key, g.workload_key, g.task_key, g.category, g.disk_key)
            > (@after_workspace::uuid, @after_app::uuid, @after_workload::uuid, @after_task::uuid, @after_category::text, @after_disk::uuid))
order by 7 desc, 1, 2, 3, 4, 5, 6
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
             when 'egress' then 'network_egress' else 'volume_storage' end)::text as dimension,
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

-- name: WorkspaceArtifactCost :one
-- What the workspace's stored artifacts cost from @since, read through the
-- ledger's source key: artifacts are metered per app, and artifacts no app
-- owns under the workspace's own id.
select coalesce(sum(l.cost_nanos), 0)::bigint
from (
    select a.id as source_id from apps a where a.workspace_id = @workspace_id
    union all
    select w.id from workspaces w where w.id = @workspace_id
) s
join ledger_entries l on l.source_kind = 'artifacts' and l.source_id = s.source_id and l.started_at >= @since;
