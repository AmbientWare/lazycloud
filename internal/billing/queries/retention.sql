-- name: StartUnfundedPeriods :many
-- Accounts that ran out of credit while storing data, read through the
-- unfunded partial index. Each starts its retention period once.
insert into unfunded_periods (user_id)
select b.user_id
from billing_balances b
join billing_accounts a on a.user_id = b.user_id
where b.balance_nanos - b.accrued_nanos <= 0 and a.complimentary_since is null
  and not exists (select 1 from unfunded_periods p where p.user_id = b.user_id)
  and exists (
      select 1
      from workspace_members o
      where o.user_id = b.user_id and o.role = 'owner'
        and (exists (select 1 from volumes v where v.workspace_id = o.workspace_id and v.state = 'active')
             or exists (select 1 from disks d where d.workspace_id = o.workspace_id and d.state = 'active')
             or exists (select 1 from artifacts r where r.workspace_id = o.workspace_id))
  )
limit @row_limit
on conflict do nothing
returning user_id, started_at;

-- name: SetUnfundedMessage :exec
update unfunded_periods set message_id = @message_id where user_id = @user_id;

-- name: OwnerEmail :one
select email from users where id = @id;

-- name: EndFundedPeriods :many
-- Credit restored or charges waived: the data stays.
delete from unfunded_periods p
using billing_balances b, billing_accounts a
where b.user_id = p.user_id and a.user_id = p.user_id
  and (b.balance_nanos - b.accrued_nanos > 0 or a.complimentary_since is not null)
returning p.message_id;

-- name: ExpiredUnfundedWorkspaces :many
-- Workspaces whose owner stayed without credit through the period.
select p.user_id, o.workspace_id
from unfunded_periods p
join workspace_members o on o.user_id = p.user_id and o.role = 'owner'
where p.started_at <= now() - make_interval(days => @days::int)
order by p.user_id, o.workspace_id;

-- name: EndUnfundedPeriod :exec
delete from unfunded_periods where user_id = @user_id;
