-- name: GrantCredit :one
-- A grant is written once per source; the account becomes due so the new
-- credit covers any negative balance first.
with lot as (
    insert into credit_lots (user_id, kind, source, amount_nanos, effective_at, expires_at)
    values (@user_id, @kind, @source, @amount_nanos, @effective_at, sqlc.narg(expires_at))
    on conflict (user_id, source) do nothing
    returning id
), due as (
    update billing_balances set due = true where user_id = @user_id and exists (select 1 from lot)
)
select id from lot;

-- name: LotBySource :one
select id from credit_lots where user_id = @user_id and source = @source;

-- name: ReverseCredit :exec
-- Refunded or disputed credit is taken back; a lot already spent past it
-- goes negative and the next rollup covers it from other credit.
with lot as (
    update credit_lots set reversed_nanos = least(@reversed_nanos::bigint, amount_nanos)
    where id = @id and reversed_nanos <> least(@reversed_nanos::bigint, amount_nanos)
    returning user_id
)
update billing_balances set due = true where user_id in (select user_id from lot);
