-- name: GrantCredit :one
-- A grant is written once per source; no row when the source was granted.
insert into credit_lots (user_id, kind, source, amount_nanos, effective_at, expires_at)
values (@user_id, @kind, @source, @amount_nanos, @effective_at, sqlc.narg(expires_at))
on conflict (user_id, source) do nothing
returning id;

-- name: MarkBalancesDue :exec
-- The accounts get a rollup on its next pass.
update billing_balances set due = true where user_id = any(@user_ids::uuid[]);

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
