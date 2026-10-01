-- name: AdminAccounts :many
-- Users after the cursor, narrowed in SQL so paging never hides a match.
-- Users billing has no account for yet come back with null billing fields.
select u.id, u.email, u.display_name, u.avatar_url, u.github_login, u.is_admin, u.status as user_status, u.created_at,
       a.terms_version, a.status, a.payment_method_attached_at, a.complimentary_since,
       coalesce((select sum(h.cost_nanos) from billing_hours h where h.user_id = u.id and h.hour >= @since::timestamptz), 0)::bigint
           as recent_cost_nanos
from users u
left join billing_accounts a on a.user_id = u.id
where u.id > @after
  and (@pattern::text = '' or u.email ilike @pattern::text or u.display_name ilike @pattern::text or u.github_login ilike @pattern::text)
  and (sqlc.narg(is_admin)::bool is null or u.is_admin = sqlc.narg(is_admin)::bool)
  and (sqlc.narg(user_status)::text is null or u.status = sqlc.narg(user_status)::text)
order by u.id
limit @row_limit;

-- name: AdminAccount :one
select u.id, u.email, u.display_name, u.avatar_url, u.github_login, u.is_admin, u.status as user_status, u.created_at,
       a.terms_version, a.status, a.payment_method_attached_at, a.complimentary_since,
       coalesce((select sum(h.cost_nanos) from billing_hours h where h.user_id = u.id and h.hour >= @since::timestamptz), 0)::bigint
           as recent_cost_nanos
from users u
left join billing_accounts a on a.user_id = u.id
where u.id = @id;
