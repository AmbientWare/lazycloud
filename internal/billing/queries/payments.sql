-- name: CustomerOf :one
select a.stripe_customer_id, u.email
from billing_accounts a
join users u on u.id = a.user_id
where a.user_id = @user_id;

-- name: SetCustomer :one
-- The first customer recorded wins; a concurrent request that created
-- another reads this one back.
update billing_accounts
set stripe_customer_id = coalesce(stripe_customer_id, sqlc.arg(customer)::text), updated_at = now()
where user_id = @user_id
returning stripe_customer_id::text;

-- name: AccountByCustomer :one
select user_id from billing_accounts where stripe_customer_id = sqlc.arg(customer)::text;

-- name: SetCard :exec
update billing_accounts
set payment_method_attached_at = case when @present::bool then coalesce(payment_method_attached_at, now()) end,
    updated_at = now()
where user_id = @user_id;

-- name: InsertManualPurchase :one
insert into credit_purchases (user_id, kind, request_key, amount_nanos, success_url, cancel_url)
values (@user_id, 'manual', @request_key, @amount_nanos, @success_url, @cancel_url)
on conflict (user_id, request_key) where request_key is not null do nothing
returning id;

-- name: PurchaseByRequest :one
select * from credit_purchases where user_id = @user_id and request_key = @request_key;

-- name: Purchase :one
select * from credit_purchases where id = @id;

-- name: LockPurchase :one
select * from credit_purchases where id = @id for update;

-- name: PurchaseByCheckout :one
select id from credit_purchases where checkout_session_id = sqlc.arg(checkout_session_id)::text;

-- name: PurchaseByIntent :one
select id from credit_purchases where payment_intent_id = sqlc.arg(payment_intent_id)::text;

-- name: SetCheckout :exec
-- The session is the purchase's until it settles. Until the session
-- expires, Stripe's delivery settles the purchase, so the sweep waits.
update credit_purchases
set checkout_session_id = @checkout_session_id, checkout_url = @checkout_url, checkout_expires_at = @checkout_expires_at,
    next_attempt_at = @next_attempt_at, updated_at = now()
where id = @id and checkout_session_id is null;

-- name: SettlePurchase :exec
update credit_purchases
set status = @status, payment_intent_id = coalesce(payment_intent_id, sqlc.narg(payment_intent_id)::text),
    lot_id = coalesce(lot_id, sqlc.narg(lot_id)::uuid), reversed_nanos = @reversed_nanos,
    funded_at = coalesce(funded_at, sqlc.narg(funded_at)::timestamptz), last_error = '',
    next_attempt_at = @next_attempt_at, updated_at = now()
where id = @id;

-- name: RetryPurchase :exec
update credit_purchases
set attempts = attempts + 1, last_error = @last_error, next_attempt_at = @next_attempt_at, updated_at = now()
where id = @id;

-- name: DuePurchases :many
select id from credit_purchases
where status = 'pending' and next_attempt_at <= now()
order by next_attempt_at
limit @row_limit;

-- name: InsertAutomaticPurchase :one
-- One automatic payment at a time; a second request while one is pending
-- inserts nothing.
insert into credit_purchases (user_id, kind, amount_nanos)
values (@user_id, 'automatic', @amount_nanos)
on conflict (user_id) where kind = 'automatic' and status = 'pending' do nothing
returning id;

-- name: DueReloads :many
-- Accounts with automatic reload on, a saved card, and a balance at or
-- below their threshold, read through the reload partial index.
select a.user_id, a.reload_amount_cents
from billing_accounts a
join billing_balances b on b.user_id = a.user_id
where a.reload_enabled and a.reload_paused_purchase_id is null
  and a.payment_method_attached_at is not null and a.stripe_customer_id is not null
  and a.status = 'active' and a.complimentary_since is null
  and b.balance_nanos - b.accrued_nanos <= a.reload_threshold_cents::bigint * 10000000
  and not exists (
      select 1 from credit_purchases p where p.user_id = a.user_id and p.kind = 'automatic' and p.status = 'pending'
  )
limit @row_limit;

-- name: PauseReload :exec
update billing_accounts
set reload_paused_purchase_id = @purchase_id, reload_pause_reason = @reason, updated_at = now()
where user_id = @user_id and reload_paused_purchase_id is null;
