-- name: AccountView :one
select a.terms_version, a.status, a.stripe_customer_id, a.period_started_at, a.period_ended_at,
       a.scheduled_terms_version, a.scheduled_change_at, a.payment_method_attached_at, a.complimentary_since,
       a.monthly_usage_limit_nanos, a.reload_enabled, a.reload_threshold_cents, a.reload_amount_cents,
       a.reload_paused_purchase_id, a.reload_pause_reason,
       b.balance_nanos, b.accrued_nanos, b.month_spent_nanos, b.month_started_at,
       exists (select 1 from plan_changes p where p.user_id = a.user_id and p.state = 'open')::bool as plan_change_pending,
       pending.id as pending_purchase_id,
       (select coalesce(sum(p.amount_nanos), 0) from credit_purchases p
        where p.user_id = a.user_id and p.kind = 'automatic' and p.status in ('pending', 'succeeded')
          and p.created_at >= date_trunc('month', clock.now, 'UTC'))::bigint as month_automatic_nanos,
       clock.now
from billing_accounts a
join billing_balances b on b.user_id = a.user_id
-- Neki's router fails to bind now() in a select list beside these
-- subqueries, so the instant comes from a joined row.
cross join (select now()::timestamptz as now) clock
-- At most one: credit_purchases_automatic_open.
left join credit_purchases pending
    on pending.user_id = a.user_id and pending.kind = 'automatic' and pending.status = 'pending'
where a.user_id = @user_id;

-- name: SetPreferences :exec
update billing_accounts
set monthly_usage_limit_nanos = sqlc.narg(monthly_usage_limit_nanos), reload_enabled = @reload_enabled,
    reload_threshold_cents = @reload_threshold_cents, reload_amount_cents = @reload_amount_cents, updated_at = now()
where user_id = @user_id;

-- name: LockAccount :one
select terms_version, status, stripe_customer_id, stripe_subscription_id, payment_method_attached_at,
       complimentary_since, scheduled_terms_version, reload_paused_purchase_id
from billing_accounts where user_id = @user_id for update;

-- name: ResumeReload :exec
update billing_accounts set reload_paused_purchase_id = null, reload_pause_reason = null, updated_at = now()
where user_id = @user_id;

-- name: SetComplimentary :execrows
update billing_accounts a
set complimentary_since = case when @complimentary::bool then coalesce(a.complimentary_since, now()) end,
    updated_at = now()
where a.user_id = @user_id;

-- name: AccountUser :one
select id, email from users where id = @id;

-- name: CustomDomainCount :one
select count(*)::int from custom_domains where user_id = @user_id;
