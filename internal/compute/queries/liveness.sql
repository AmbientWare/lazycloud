-- name: StaleHosts :many
-- Online hosts silent for longer than the liveness timeout, in (last_seen_at,
-- id) order after the cursor. Reads the hosts_online partial index. A reserve
-- stopping or stopped is asleep, not lost.
select id, last_seen_at
from hosts
where state = 'online'
  and last_seen_at < now() - make_interval(secs => @timeout_seconds::float8)
  and (updating_until is null or updating_until < now())
  and not (phase in ('stopping', 'stopped') and reserve_mode is not null)
  and (last_seen_at, id) > (@after_seen_at::timestamptz, @after_id::uuid)
order by last_seen_at, id
limit @batch_size;

-- name: MarkHostLost :execrows
-- Rechecks staleness under the row lock, so a host that reconnected, or went
-- to sleep in the reserve, after the scan stays online.
update hosts
set state = 'lost'
where id = @id
  and state = 'online'
  and last_seen_at < now() - make_interval(secs => @timeout_seconds::float8)
  and (updating_until is null or updating_until < now())
  and not (phase in ('stopping', 'stopped') and reserve_mode is not null);
