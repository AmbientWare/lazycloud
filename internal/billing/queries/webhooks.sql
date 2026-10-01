-- name: StoreEvent :exec
-- Stripe retries deliveries; the event id records each once.
insert into stripe_events (id, type, object_id, customer_id)
values (@id, @type, @object_id, sqlc.narg(customer_id))
on conflict (id) do nothing;

-- name: ClaimEvents :many
update stripe_events
set attempts = attempts + 1, lease_until = now() + interval '5 minutes'
where id in (
    select e.id from stripe_events e
    where e.processed_at is null and e.next_attempt_at <= now() and (e.lease_until is null or e.lease_until < now())
    order by e.received_at
    limit @row_limit
    for update skip locked
)
returning id, type, object_id, customer_id, attempts;

-- name: FinishEvent :exec
update stripe_events set processed_at = now(), lease_until = null, last_error = @last_error where id = @id;

-- name: RetryEvent :exec
update stripe_events set lease_until = null, next_attempt_at = @next_attempt_at, last_error = @last_error where id = @id;

-- name: PurgeEvents :execrows
delete from stripe_events
where id in (
    select e.id from stripe_events e
    where e.processed_at is not null and e.processed_at < @before::timestamptz
    order by e.processed_at
    limit @row_limit
);
