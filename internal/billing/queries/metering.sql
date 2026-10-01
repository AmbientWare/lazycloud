-- name: TryMeteringLock :one
-- Held for the metering pass by its session, so replicas skip a pass
-- another is running.
select pg_try_advisory_lock(hashtextextended('billing-metering', 0))::bool;

-- name: ReleaseMeteringLock :exec
select pg_advisory_unlock(hashtextextended('billing-metering', 0));

-- name: MeteringClock :one
-- The pass's instant and where its look-back over stopped containers
-- starts. A first pass looks back over everything.
select now()::timestamptz as now,
       coalesce((select stopped_since from metering_state), 'epoch'::timestamptz)::timestamptz as stopped_since;

-- name: LiveMeteredContainers :many
-- Ready and draining containers: the live set, read through its partial
-- indexes rather than the container history.
select c.id, c.workspace_id, c.release_id, c.image_build_id, c.cpu_millis, c.memory_bytes,
       c.ready_at::timestamptz as ready_at, c.stopped_at, c.stop_reason, h.last_seen_at,
       w.id as workload_id, w.app_id, o.user_id as owner_id, u.billed_through,
       coalesce(u.complete, false)::bool as complete
from containers c
join workspace_members o on o.workspace_id = c.workspace_id and o.role = 'owner'
left join hosts h on h.id = c.host_id
left join releases r on r.id = c.release_id
left join workloads w on w.id = r.workload_id
left join usage_cursors u on u.container_id = c.id
where c.state <> 'stopped' and c.state in ('ready', 'draining') and c.ready_at is not null;

-- name: StoppedMeteredContainers :many
-- Containers that stopped since the look-back began and still owe their
-- last entries.
select c.id, c.workspace_id, c.release_id, c.image_build_id, c.cpu_millis, c.memory_bytes,
       c.ready_at::timestamptz as ready_at, c.stopped_at, c.stop_reason, h.last_seen_at,
       w.id as workload_id, w.app_id, o.user_id as owner_id, u.billed_through,
       coalesce(u.complete, false)::bool as complete
from containers c
join workspace_members o on o.workspace_id = c.workspace_id and o.role = 'owner'
left join hosts h on h.id = c.host_id
left join releases r on r.id = c.release_id
left join workloads w on w.id = r.workload_id
left join usage_cursors u on u.container_id = c.id
where c.ready_at is not null and c.stopped_at >= @since and not coalesce(u.complete, false);

-- name: EnsureAccounts :exec
-- Creates the accounts that do not exist yet with their trial credit and
-- a balance holding it.
with created as (
    insert into billing_accounts (user_id)
    select unnest(@user_ids::uuid[])
    on conflict do nothing
    returning user_id, created_at
), trial as (
    insert into credit_lots (user_id, kind, source, amount_nanos, effective_at, expires_at)
    select user_id, 'trial', 'trial', @trial_nanos::bigint, created_at, created_at + make_interval(days => @trial_days::int)
    from created
)
insert into billing_balances (user_id, balance_nanos, month_started_at, recheck_at)
select user_id, @trial_nanos::bigint, date_trunc('month', created_at, 'UTC'),
       least(created_at + make_interval(days => @trial_days::int), date_trunc('month', created_at, 'UTC') + interval '1 month')
from created;

-- name: InsertLedgerEntries :exec
-- Entries already written are skipped, so a repeated batch adds nothing to
-- the hourly totals. Accounts with new cost become due for a rollup.
with entry as (
    insert into ledger_entries (
        source_kind, source_id, started_at, ended_at, user_id, workspace_id, app_id, workload_id, category,
        billing_owner, rate_class, gpu_type, gpu_count, cpu_millis, memory_bytes, pricing_version,
        container_nanos, cpu_nanos, memory_nanos, gpu_nanos)
    select 'container', e.source_id, e.started_at, e.ended_at, e.user_id, e.workspace_id,
           nullif(e.app_id, '00000000-0000-0000-0000-000000000000'::uuid),
           nullif(e.workload_id, '00000000-0000-0000-0000-000000000000'::uuid),
           nullif(e.category, ''), e.billing_owner, e.rate_class, nullif(e.gpu_type, ''), e.gpu_count,
           e.cpu_millis, e.memory_bytes, e.pricing_version,
           e.container_nanos, e.cpu_nanos, e.memory_nanos, e.gpu_nanos
    from (
        select unnest(@source_ids::uuid[]) as source_id, unnest(@started_ats::timestamptz[]) as started_at,
               unnest(@ended_ats::timestamptz[]) as ended_at, unnest(@user_ids::uuid[]) as user_id,
               unnest(@workspace_ids::uuid[]) as workspace_id, unnest(@app_ids::uuid[]) as app_id,
               unnest(@workload_ids::uuid[]) as workload_id, unnest(@categories::text[]) as category,
               unnest(@billing_owners::text[]) as billing_owner, unnest(@rate_classes::text[]) as rate_class,
               unnest(@gpu_types::text[]) as gpu_type, unnest(@gpu_counts::int[]) as gpu_count,
               unnest(@cpu_millis::bigint[]) as cpu_millis, unnest(@memory_bytes::bigint[]) as memory_bytes,
               unnest(@pricing_versions::text[]) as pricing_version,
               unnest(@container_nanos::bigint[]) as container_nanos, unnest(@cpu_nanos::bigint[]) as cpu_nanos,
               unnest(@memory_nanos::bigint[]) as memory_nanos, unnest(@gpu_nanos::bigint[]) as gpu_nanos
    ) e
    on conflict (source_kind, source_id, started_at) do nothing
    returning user_id, started_at, cost_nanos
), hour as (
    insert into billing_hours (user_id, hour, cost_nanos)
    select user_id, date_trunc('hour', started_at, 'UTC'), sum(cost_nanos)::bigint
    from entry
    group by 1, 2
    on conflict (user_id, hour) do update set cost_nanos = billing_hours.cost_nanos + excluded.cost_nanos
    returning user_id
)
update billing_balances set due = true
where user_id in (select user_id from hour) and not due;

-- name: AdvanceCursors :exec
insert into usage_cursors (container_id, billed_through, complete, updated_at)
select c.id, c.through, c.complete, now()
from (select unnest(@ids::uuid[]) as id, unnest(@through::timestamptz[]) as through,
             unnest(@complete::bool[]) as complete) c
on conflict (container_id) do update
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
-- Complete cursors older than the look-back can no longer be scanned.
delete from usage_cursors
where container_id in (
    select u.container_id from usage_cursors u
    where u.complete and u.updated_at < @before
    order by u.updated_at
    limit @row_limit
);
