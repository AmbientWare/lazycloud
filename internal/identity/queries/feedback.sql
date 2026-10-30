-- name: LockFeedbackAuthor :exec
-- Serializes one account's submissions so concurrent ones count each other.
select 1 from users where id = @id for update;

-- name: RecentFeedback :one
-- Deleted feedback counts too, so deleting spam does not hand its sender
-- more sends.
select count(*) filter (where created_at > now() - interval '1 hour') as last_hour,
       count(*) as last_day
from feedback
where user_id = @user_id and created_at > now() - interval '1 day';

-- name: InsertFeedback :exec
insert into feedback (user_id, kind, message) values (@user_id, @kind, @message);

-- name: DeleteFeedback :execrows
update feedback set deleted_at = now() where id = @id and deleted_at is null;

-- name: PruneFeedback :execrows
-- A deleted row stops counting against the rate limit after a day.
delete from feedback
where id in (
    select p.id from feedback p
    where p.deleted_at < now() - interval '1 day'
    order by p.deleted_at
    limit @row_limit
);

-- name: ListFeedback :many
-- Newest first; ids are UUIDv7, so id order is creation order.
select f.id, f.kind, f.message, f.created_at,
       u.id as user_id, u.email, u.display_name, u.avatar_url, u.github_login, u.is_admin, u.status,
       u.created_at as user_created_at
from feedback f
join users u on u.id = f.user_id
where f.deleted_at is null
  and (sqlc.narg(before)::uuid is null or f.id < sqlc.narg(before)::uuid)
  and (sqlc.narg(kind)::text is null or f.kind = sqlc.narg(kind)::text)
  and (@pattern::text = '' or f.message ilike @pattern::text or u.email ilike @pattern::text
       or u.display_name ilike @pattern::text or u.github_login ilike @pattern::text)
order by f.id desc
limit @row_limit;
