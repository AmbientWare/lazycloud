-- name: TryMeteringLock :one
-- Held for the metering pass by its session, so replicas skip a pass
-- another is running.
select pg_try_advisory_lock(hashtextextended('billing-metering', 0))::bool;

-- name: ReleaseMeteringLock :exec
select pg_advisory_unlock(hashtextextended('billing-metering', 0));

-- name: MeteringClock :one
-- The pass's instant, where its look-back over stopped containers starts,
-- and observability's rollup watermark: ingest refuses samples older than
-- it, so a container's use before it is complete. A first pass looks back
-- over everything.
select now()::timestamptz as now,
       coalesce((select stopped_since from metering_state), 'epoch'::timestamptz)::timestamptz as stopped_since,
       (select rolled_through from container_metric_rollup)::timestamptz as measured_through;

-- name: LiveMeteredContainers :many
-- Ready and draining containers: the live set, read through its partial
-- indexes rather than the container history.
select c.id, c.workspace_id, c.release_id, c.image_build_id, c.cpu_millis, c.memory_bytes,
       c.gpu_count, c.gpu_type, c.rate_class, c.billing_owner,
       c.ready_at::timestamptz as ready_at, c.stopped_at, c.stop_reason, h.last_seen_at,
       w.id as workload_id, w.app_id, o.user_id as owner_id, u.billed_through,
       coalesce(u.complete, false)::bool as complete
from containers c
join workspace_members o on o.workspace_id = c.workspace_id and o.role = 'owner'
left join hosts h on h.id = c.host_id
left join releases r on r.id = c.release_id
left join workloads w on w.id = r.workload_id
left join usage_cursors u on u.source_kind = 'container' and u.source_id = c.id
where c.state <> 'stopped' and c.state in ('ready', 'draining') and c.ready_at is not null;

-- name: StoppedMeteredContainers :many
-- Containers that stopped since the look-back began and still owe their
-- last entries.
select c.id, c.workspace_id, c.release_id, c.image_build_id, c.cpu_millis, c.memory_bytes,
       c.gpu_count, c.gpu_type, c.rate_class, c.billing_owner,
       c.ready_at::timestamptz as ready_at, c.stopped_at, c.stop_reason, h.last_seen_at,
       w.id as workload_id, w.app_id, o.user_id as owner_id, u.billed_through,
       coalesce(u.complete, false)::bool as complete
from containers c
join workspace_members o on o.workspace_id = c.workspace_id and o.role = 'owner'
left join hosts h on h.id = c.host_id
left join releases r on r.id = c.release_id
left join workloads w on w.id = r.workload_id
left join usage_cursors u on u.source_kind = 'container' and u.source_id = c.id
where c.ready_at is not null and c.stopped_at >= @since and not coalesce(u.complete, false);

-- name: InsertAccounts :many
-- The accounts that did not exist yet.
insert into billing_accounts (user_id)
select unnest(@user_ids::uuid[])
on conflict do nothing
returning user_id, created_at;

-- name: InsertTrialCredit :exec
-- The trial credit of accounts InsertAccounts created.
insert into credit_lots (user_id, kind, source, amount_nanos, effective_at, expires_at)
select a.user_id, 'trial', 'trial', @trial_nanos::bigint, a.created_at, a.created_at + make_interval(days => @trial_days::int)
from (select unnest(@user_ids::uuid[]) as user_id, unnest(@created_ats::timestamptz[]) as created_at) a;

-- name: InsertTrialBalances :exec
-- The balances, holding the trial credit, of accounts InsertAccounts created.
insert into billing_balances (user_id, balance_nanos, month_started_at, recheck_at)
select a.user_id, @trial_nanos::bigint, date_trunc('month', a.created_at, 'UTC'),
       least(a.created_at + make_interval(days => @trial_days::int), date_trunc('month', a.created_at, 'UTC') + interval '1 month')
from (select unnest(@user_ids::uuid[]) as user_id, unnest(@created_ats::timestamptz[]) as created_at) a;

-- name: InsertLedgerEntries :many
-- Entries already written are skipped and not returned, so a repeated batch
-- adds nothing to the hourly totals.
insert into ledger_entries (
    source_kind, source_id, started_at, ended_at, user_id, workspace_id, app_id, workload_id, category,
    billing_owner, rate_class, gpu_type, gpu_count, cpu_millis, memory_bytes, pricing_version,
    container_nanos, cpu_nanos, memory_nanos, gpu_nanos, stored_bytes, attached_bytes, storage_nanos, attached_nanos,
    egress_bytes, egress_nanos)
select e.source_kind, e.source_id, e.started_at, e.ended_at, e.user_id, e.workspace_id,
       nullif(e.app_id, '00000000-0000-0000-0000-000000000000'::uuid),
       nullif(e.workload_id, '00000000-0000-0000-0000-000000000000'::uuid),
       nullif(e.category, ''), e.billing_owner, e.rate_class, nullif(e.gpu_type, ''), e.gpu_count,
       e.cpu_millis, e.memory_bytes, e.pricing_version,
       e.container_nanos, e.cpu_nanos, e.memory_nanos, e.gpu_nanos,
       e.stored_bytes, e.attached_bytes, e.storage_nanos, e.attached_nanos, e.egress_bytes, e.egress_nanos
from (
    select unnest(@source_kinds::text[]) as source_kind, unnest(@source_ids::uuid[]) as source_id, unnest(@started_ats::timestamptz[]) as started_at,
           unnest(@ended_ats::timestamptz[]) as ended_at, unnest(@user_ids::uuid[]) as user_id,
           unnest(@workspace_ids::uuid[]) as workspace_id, unnest(@app_ids::uuid[]) as app_id,
           unnest(@workload_ids::uuid[]) as workload_id, unnest(@categories::text[]) as category,
           unnest(@billing_owners::text[]) as billing_owner, unnest(@rate_classes::text[]) as rate_class,
           unnest(@gpu_types::text[]) as gpu_type, unnest(@gpu_counts::int[]) as gpu_count,
           unnest(@cpu_millis::bigint[]) as cpu_millis, unnest(@memory_bytes::bigint[]) as memory_bytes,
           unnest(@pricing_versions::text[]) as pricing_version,
           unnest(@container_nanos::bigint[]) as container_nanos, unnest(@cpu_nanos::bigint[]) as cpu_nanos,
           unnest(@memory_nanos::bigint[]) as memory_nanos, unnest(@gpu_nanos::bigint[]) as gpu_nanos,
           unnest(@stored_bytes::bigint[]) as stored_bytes, unnest(@attached_bytes::bigint[]) as attached_bytes,
           unnest(@storage_nanos::bigint[]) as storage_nanos, unnest(@attached_nanos::bigint[]) as attached_nanos,
           unnest(@egress_bytes::bigint[]) as egress_bytes, unnest(@egress_nanos::bigint[]) as egress_nanos
) e
on conflict (source_kind, source_id, started_at) do nothing
returning user_id, started_at, cost_nanos::bigint as cost_nanos;

-- name: AddLedgerHours :exec
-- Adds the entries InsertLedgerEntries wrote to their hourly totals.
insert into billing_hours (user_id, hour, cost_nanos)
select e.user_id, date_trunc('hour', e.started_at, 'UTC'), sum(e.cost_nanos)::bigint
from (
    select unnest(@user_ids::uuid[]) as user_id, unnest(@started_ats::timestamptz[]) as started_at,
           unnest(@cost_nanos::bigint[]) as cost_nanos
) e
group by 1, 2
on conflict (user_id, hour) do update set cost_nanos = billing_hours.cost_nanos + excluded.cost_nanos;

-- name: AdvanceCursors :exec
insert into usage_cursors (source_kind, source_id, billed_through, complete, updated_at)
select c.kind, c.id, c.through, c.complete, now()
from (select unnest(@kinds::text[]) as kind, unnest(@ids::uuid[]) as id, unnest(@through::timestamptz[]) as through,
             unnest(@complete::bool[]) as complete) c
on conflict (source_kind, source_id) do update
set billed_through = greatest(usage_cursors.billed_through, excluded.billed_through),
    complete = usage_cursors.complete or excluded.complete,
    updated_at = now();

-- name: SetAccrued :exec
-- The open-interval cost and live container count of every account with
-- live containers; every other account's are zeroed.
with live as (
    select a.user_id, a.accrued, a.live
    from (select unnest(@user_ids::uuid[]) as user_id, unnest(@accrued::bigint[]) as accrued,
                 unnest(@live::int[]) as live) a
), zeroed as (
    update billing_balances b set accrued_nanos = 0, live_containers = 0
    where b.live_containers > 0 and b.user_id not in (select user_id from live)
)
update billing_balances b set accrued_nanos = live.accrued, live_containers = live.live
from live
where b.user_id = live.user_id and (b.accrued_nanos <> live.accrued or b.live_containers <> live.live);

-- name: SetStoppedSince :exec
insert into metering_state (singleton, stopped_since) values (true, @stopped_since)
on conflict (singleton) do update set stopped_since = excluded.stopped_since;

-- name: PruneCursors :execrows
-- Complete cursors older than the look-back can no longer be scanned, and
-- a storage source unseen for a day is gone.
delete from usage_cursors u
using (
    select c.source_kind, c.source_id from usage_cursors c
    where (c.complete and c.updated_at < @complete_before::timestamptz)
       or (c.source_kind <> 'container' and c.updated_at < @gone_before::timestamptz)
    limit @row_limit
) old
where u.source_kind = old.source_kind and u.source_id = old.source_id;

-- name: MeteredVolumes :many
-- Every volume row: active ones store bytes, deleting ones until they were
-- deleted. The sweep removes a row once its files are gone.
select v.id, v.workspace_id, v.size_bytes, v.created_at, v.deleted_at, o.user_id as owner_id,
       u.billed_through, exists (select 1 from unfunded_periods p where p.user_id = o.user_id)::bool as waived
from volumes v
join workspace_members o on o.workspace_id = v.workspace_id and o.role = 'owner'
left join usage_cursors u on u.source_kind = 'volume' and u.source_id = v.id;

-- name: MeteredDisks :many
-- Every disk row: it stores bytes for its life and holds its declared size
-- while a container holds it or released it since the cursor.
select d.id, d.workspace_id, d.stored_bytes, d.size_bytes, d.created_at, d.deleted_at,
       coalesce(d.holder_container_id is not null or d.released_at > u.billed_through, false)::bool as held,
       o.user_id as owner_id, u.billed_through,
       exists (select 1 from unfunded_periods p where p.user_id = o.user_id)::bool as waived
from disks d
join workspace_members o on o.workspace_id = d.workspace_id and o.role = 'owner'
left join usage_cursors u on u.source_kind = 'disk' and u.source_id = d.id;

-- name: MeteredArtifacts :many
-- Stored artifact bytes per app, or per workspace for artifacts no app
-- owns, through the artifact listing index.
select a.workspace_id, a.app_id, coalesce(a.app_id, a.workspace_id)::uuid as source_id,
       sum(a.size_bytes)::bigint as stored_bytes, min(a.stored_at)::timestamptz as first_stored_at,
       o.user_id as owner_id, u.billed_through,
       exists (select 1 from unfunded_periods p where p.user_id = o.user_id)::bool as waived
from artifacts a
join workspace_members o on o.workspace_id = a.workspace_id and o.role = 'owner'
left join usage_cursors u on u.source_kind = 'artifacts' and u.source_id = coalesce(a.app_id, a.workspace_id)
where a.state = 'stored'
group by a.workspace_id, a.app_id, o.user_id, u.billed_through;

-- name: RecordEgress :exec
insert into egress_quarters (workspace_id, app_id, workload_id, quarter, bytes)
values (@workspace_id, @app_id, @workload_id, @quarter, @bytes)
on conflict (workspace_id, app_id, workload_id, quarter) do update set bytes = egress_quarters.bytes + excluded.bytes;

-- name: ClosedEgress :many
-- Quarters that closed before the edge's lag, locked so one pass prices
-- them; the pass deletes them with their entries.
select q.workspace_id, q.app_id, q.workload_id, q.quarter, q.bytes, o.user_id as owner_id
from egress_quarters q
join workspace_members o on o.workspace_id = q.workspace_id and o.role = 'owner'
where q.quarter < @before::timestamptz
order by q.quarter
limit @row_limit
for update of q skip locked;

-- name: DeleteEgress :exec
delete from egress_quarters q
using (select unnest(@workspace_ids::uuid[]) as workspace_id, unnest(@app_ids::uuid[]) as app_id,
              unnest(@workload_ids::uuid[]) as workload_id, unnest(@quarters::timestamptz[]) as quarter) d
where q.workspace_id = d.workspace_id and q.app_id = d.app_id and q.workload_id = d.workload_id and q.quarter = d.quarter;

-- name: MeasuredContainerUse :many
-- CPU core-seconds and memory byte-seconds each container used per metering
-- period of [start_at, end_at), read from observability's container
-- metrics: the samples while they are kept, and for minutes whose samples
-- expired the rollup's minutes, so no sample counts twice. A sample covers
-- the interval before it; a minute's memory is its peak.
with want as (
    select unnest(@container_ids::uuid[]) as id,
           unnest(@start_ats::timestamptz[]) as start_at,
           unnest(@end_ats::timestamptz[]) as end_at
), used as (
    select s.container_id, s.sampled_at as at, s.cpu_usage_usec,
           s.memory_rss_bytes::float8 * s.interval_ms / 1000 as memory_byte_seconds
    from want
    join container_metric_samples s on s.container_id = want.id
         and s.sampled_at >= want.start_at and s.sampled_at < want.end_at
    union all
    select m.container_id, m.minute as at, m.cpu_usage_usec,
           m.memory_rss_bytes::float8 * m.interval_ms / 1000 as memory_byte_seconds
    from want
    join container_metric_minutes m on m.container_id = want.id
         and m.minute >= want.start_at and m.minute < want.end_at
    where not exists (
        select 1 from container_metric_samples s
        where s.container_id = m.container_id and s.sampled_at >= m.minute and s.sampled_at < m.minute + interval '1 minute')
)
select container_id,
       date_bin(@period::interval, at, 'epoch'::timestamptz)::timestamptz as period,
       (sum(cpu_usage_usec) / 1e6)::float8 as core_seconds,
       sum(memory_byte_seconds)::float8 as memory_byte_seconds
from used
group by 1, 2;
