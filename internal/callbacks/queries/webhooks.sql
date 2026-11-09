-- name: LockWorkspaceWebhooks :one
-- Locks the workspace's webhooks for a create, which the limit counts, and
-- returns how many there are.
select (select count(*) from webhooks h where h.workspace_id = w.id)::int as webhooks
from workspaces w
where w.id = @workspace_id
for update of w;

-- name: InsertWebhook :one
insert into webhooks (workspace_id, apps, url, events, description, enabled)
values (@workspace_id, @apps::text[], @url, @events::text[], @description, @enabled)
returning *;

-- name: ListWebhooks :many
select * from webhooks
where workspace_id = @workspace_id and id > @after::uuid
order by id
limit @max_rows;

-- name: GetWebhook :one
select * from webhooks where workspace_id = @workspace_id and id = @id;

-- name: UpdateWebhook :one
-- Sets the named fields; an empty apps takes the webhook to every app.
update webhooks
set url = coalesce(sqlc.narg(url), url),
    apps = coalesce(sqlc.narg(apps)::text[], apps),
    events = coalesce(sqlc.narg(events)::text[], events),
    description = coalesce(sqlc.narg(description), description),
    enabled = coalesce(sqlc.narg(enabled), enabled)
where workspace_id = @workspace_id and id = @id
returning *;

-- name: DeleteWebhook :execrows
delete from webhooks where workspace_id = @workspace_id and id = @id;

-- name: InsertTestDelivery :one
-- A webhook.test event for one webhook.
insert into webhook_deliveries (webhook_id, workspace_id, event_id, event, data)
select h.id, h.workspace_id, uuidv7(), 'webhook.test', jsonb_build_object('webhook_id', h.id)
from webhooks h
where h.workspace_id = @workspace_id and h.id = @id
returning *;

-- name: ListWebhookDeliveries :many
-- The webhook's deliveries below the cursor id, newest first.
select d.* from webhook_deliveries d
join webhooks h on h.id = d.webhook_id
where h.workspace_id = @workspace_id and d.webhook_id = @webhook_id and d.id < @before::bigint
order by d.id desc
limit @max_rows;

-- name: ClaimDueWebhookDeliveries :many
-- Leases due deliveries as ClaimDueCallbacks leases callbacks: in
-- per-workspace turns, moving next_attempt_at past the delivery timeout and
-- counting the attempt in deliveries, which fences a late outcome.
with recursive spaces as (
    (select d.workspace_id from webhook_deliveries d
     where d.state = 'pending' and d.next_attempt_at <= now()
     order by d.workspace_id
     limit 1)
    union all
    select (select d.workspace_id from webhook_deliveries d
            where d.state = 'pending' and d.next_attempt_at <= now() and d.workspace_id > s.workspace_id
            order by d.workspace_id
            limit 1)
    from spaces s
    where s.workspace_id is not null
), turns as (
    select q.id, q.next_attempt_at, q.turn
    from spaces s
    cross join lateral (
        select d.id, d.next_attempt_at, row_number() over (order by d.next_attempt_at, d.id) as turn
        from webhook_deliveries d
        where d.workspace_id = s.workspace_id and d.state = 'pending' and d.next_attempt_at <= now()
        order by d.next_attempt_at, d.id
        limit @batch_size
    ) q
    where s.workspace_id is not null
), picked as (
    select p.id from webhook_deliveries p
    where p.id in (select t.id from turns t order by t.turn, t.next_attempt_at, t.id limit @batch_size)
      and p.state = 'pending' and p.next_attempt_at <= now()
    for update skip locked
)
update webhook_deliveries d
set next_attempt_at = now() + make_interval(secs => @lease_seconds::float8),
    deliveries = d.deliveries + 1
from picked, webhooks h, workspaces w
where d.id = picked.id and h.id = d.webhook_id and w.id = d.workspace_id
returning d.id, d.deliveries, d.workspace_id, w.name as workspace_name, h.url, d.event_id, d.event, d.data, d.created_at;

-- name: FinishWebhookDelivery :exec
update webhook_deliveries
set state = @state, response_status = sqlc.narg(response_status), last_error = sqlc.narg(last_error), finished_at = now()
where id = @id and deliveries = @deliveries and state = 'pending';

-- name: RetryWebhookDelivery :exec
update webhook_deliveries
set next_attempt_at = now() + make_interval(secs => @delay_seconds::float8),
    response_status = sqlc.narg(response_status), last_error = @last_error
where id = @id and deliveries = @deliveries and state = 'pending';

-- name: PurgeFinishedWebhookDeliveries :execrows
delete from webhook_deliveries
where id in (
    select f.id from webhook_deliveries f
    where f.state <> 'pending' and f.finished_at < now() - make_interval(secs => @retain_seconds::float8)
    limit @batch_size
);

-- name: NextWebhookDeliveryAt :many
-- When the earliest pending delivery is due; no row without one.
select next_attempt_at from webhook_deliveries where state = 'pending' order by next_attempt_at limit 1;
