-- name: ClaimRollup :one
-- One account whose balance is due, locked for this rollup; replicas pass
-- over accounts another holds. The balance row lock is the account's rollup
-- lock: it also orders the rollup against metering marking it due again.
select b.user_id, a.complimentary_since, now()::timestamptz as now
from billing_balances b
join billing_accounts a on a.user_id = b.user_id
where b.due or b.recheck_at <= now()
limit 1
for update of b skip locked;

-- name: LockBalance :one
select b.user_id, a.complimentary_since, now()::timestamptz as now
from billing_balances b
join billing_accounts a on a.user_id = b.user_id
where b.user_id = @user_id
for update of b;

-- name: UncoveredHours :many
select hour, cost_nanos, credited_nanos, subscription_nanos, waived_nanos
from billing_hours
where user_id = @user_id and credited_nanos + waived_nanos < cost_nanos
order by hour;

-- name: OpenLots :many
-- Lots with credit left, or overdrawn by a reversal, in the order they are
-- spent: earliest expiry first, then oldest.
select id, kind, (amount_nanos - reversed_nanos - spent_nanos)::bigint as remaining_nanos, effective_at, expires_at
from credit_lots
where user_id = @user_id and spent_nanos <> amount_nanos - reversed_nanos
order by expires_at nulls last, effective_at, id;

-- name: SpendLots :exec
update credit_lots l set spent_nanos = l.spent_nanos + d.delta
from (select unnest(@ids::uuid[]) as id, unnest(@deltas::bigint[]) as delta) d
where l.id = d.id;

-- name: CoverHours :exec
update billing_hours h
set credited_nanos = h.credited_nanos + d.credited,
    subscription_nanos = h.subscription_nanos + d.subscription,
    waived_nanos = h.waived_nanos + d.waived
from (select unnest(@hours::timestamptz[]) as hour, unnest(@credited::bigint[]) as credited,
             unnest(@subscription::bigint[]) as subscription, unnest(@waived::bigint[]) as waived) d
where h.user_id = @user_id and h.hour = d.hour;

-- name: MonthSpent :one
select coalesce(sum(cost_nanos), 0)::bigint
from billing_hours
where user_id = @user_id and hour >= @month_started_at::timestamptz and hour < @month_ended_at::timestamptz;

-- name: SetBalance :exec
update billing_balances
set balance_nanos = @balance_nanos, month_started_at = @month_started_at, month_spent_nanos = @month_spent_nanos,
    recheck_at = @recheck_at, due = false, rolled_at = now()
where user_id = @user_id;

-- name: UnfundedAccounts :many
-- Accounts with live containers that may not run them: no credit left, the
-- monthly limit reached or a payment past due.
select b.user_id,
       (b.balance_nanos - b.accrued_nanos <= 0)::bool as no_credit,
       (a.monthly_usage_limit_nanos is not null
        and b.month_spent_nanos + b.accrued_nanos >= a.monthly_usage_limit_nanos)::bool as over_limit,
       (a.status = 'past_due')::bool as past_due
from billing_balances b
join billing_accounts a on a.user_id = b.user_id
where b.live_containers > 0
  and a.complimentary_since is null
  and (b.balance_nanos - b.accrued_nanos <= 0
       or a.status = 'past_due'
       or (a.monthly_usage_limit_nanos is not null
           and b.month_spent_nanos + b.accrued_nanos >= a.monthly_usage_limit_nanos));

-- name: OwnedWorkspaces :many
select workspace_id from workspace_members where user_id = @user_id and role = 'owner' order by workspace_id;
