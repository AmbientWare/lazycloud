-- name: InsertPlanChange :one
-- Claimed by the request that inserts it, which calls Stripe next.
insert into plan_changes (user_id, from_terms, to_terms, attempts, lease_until)
values (@user_id, @from_terms, @to_terms, 1, now() + interval '5 minutes')
returning id, user_id, from_terms, to_terms, attempts, created_at;

-- name: ClaimPlanChanges :many
-- Open changes whose request died or whose last attempt failed, leased so
-- one settler calls Stripe at a time.
update plan_changes
set attempts = attempts + 1, lease_until = now() + interval '5 minutes'
where id in (
    select p.id from plan_changes p
    where p.state = 'open' and p.next_attempt_at <= now() and (p.lease_until is null or p.lease_until < now())
    order by p.next_attempt_at
    limit @row_limit
    for update skip locked
)
returning id, user_id, from_terms, to_terms, attempts, created_at;

-- name: FinishPlanChange :exec
update plan_changes set state = @state, error = @error, finished_at = now(), lease_until = null
where id = @id and state = 'open';

-- name: RetryPlanChange :exec
update plan_changes set lease_until = null, next_attempt_at = @next_attempt_at, error = @error
where id = @id and state = 'open';

-- name: StripeIdentity :one
select stripe_customer_id, stripe_subscription_id from billing_accounts where user_id = @user_id;

-- name: LockSubscriber :one
select stripe_subscription_id from billing_accounts where user_id = @user_id for update;

-- name: SetSubscription :exec
update billing_accounts
set terms_version = @terms_version, status = @status, stripe_subscription_id = sqlc.narg(subscription_id),
    period_started_at = sqlc.narg(period_started_at), period_ended_at = sqlc.narg(period_ended_at),
    scheduled_terms_version = sqlc.narg(scheduled_terms_version), scheduled_change_at = sqlc.narg(scheduled_change_at),
    updated_at = now()
where user_id = @user_id;
