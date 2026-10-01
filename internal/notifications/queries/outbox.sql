-- name: EnqueueEmail :one
insert into email_outbox (recipient, subject, html, body_text)
values (@recipient, @subject, @html, @body_text)
returning id;

-- name: DiscardQueued :execrows
-- A message still waiting is never sent. One being delivered keeps its lease
-- and settles as sent or failed only while it is queued, so it stays
-- discarded.
update email_outbox
set state = 'discarded', settled_at = now(), lease_until = null
where id = any(@ids::uuid[]) and state = 'queued';

-- name: DeliveryStates :many
select id, state from email_outbox where id = any(@ids::uuid[]);

-- name: ClaimDue :many
-- Takes due messages for one delivery pass. attempts counts the claim and a
-- settle names it, so a pass whose lease expired cannot settle a message
-- another pass claimed again.
update email_outbox o
set attempts = o.attempts + 1, lease_until = now() + make_interval(secs => @lease_seconds::float8)
where o.id in (
    select d.id from email_outbox d
    where d.state = 'queued' and d.next_attempt_at <= now()
      and (d.lease_until is null or d.lease_until <= now())
    order by d.next_attempt_at, d.id
    limit @batch_size
    for update skip locked
)
returning o.id, o.recipient, o.subject, o.html, o.body_text, o.attempts;

-- name: MarkSent :execrows
update email_outbox
set state = 'sent', provider_message_id = @provider_message_id, settled_at = now(),
    lease_until = null, last_error = ''
where id = @id and attempts = @attempts and state = 'queued';

-- name: MarkRetry :execrows
update email_outbox
set next_attempt_at = now() + make_interval(secs => @delay_seconds::float8), lease_until = null,
    last_error = @last_error
where id = @id and attempts = @attempts and state = 'queued';

-- name: MarkFailed :execrows
update email_outbox
set state = 'failed', settled_at = now(), lease_until = null, last_error = @last_error
where id = @id and attempts = @attempts and state = 'queued';

-- name: RecordDelivery :execrows
-- Provider events arrive out of order: an older event never replaces a newer
-- one, and only messages the provider accepted take delivery states.
update email_outbox
set state = @state, delivery_event_at = @occurred_at,
    last_error = case when @detail::text = '' then last_error else @detail::text end
where provider_message_id = @provider_message_id
  and state in ('sent', 'delivered', 'bounced', 'complained')
  and (delivery_event_at is null or delivery_event_at <= @occurred_at);

-- name: PurgeBodies :execrows
-- Bodies carry working invitation links, so settled messages keep only their
-- delivery record.
update email_outbox
set html = '', body_text = '', purged_at = now()
where id in (
    select p.id from email_outbox p
    where p.purged_at is null and p.settled_at < now() - make_interval(secs => @retention_seconds::float8)
    order by p.settled_at
    limit @batch_size
);
