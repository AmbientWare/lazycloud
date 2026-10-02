-- name: ClaimDueCallbacks :many
-- Leases due callbacks to this deliverer: next_attempt_at moves past the
-- delivery timeout and deliveries counts the attempt, which fences a late
-- outcome from an earlier lease.
update task_callbacks c
set next_attempt_at = now() + make_interval(secs => @lease_seconds::float8),
    deliveries = c.deliveries + 1
where c.id in (
    select d.id from task_callbacks d
    where d.state = 'pending' and d.next_attempt_at <= now()
    order by d.next_attempt_at, d.id
    limit @batch_size
    for update skip locked
)
returning c.id, c.deliveries;

-- name: CallbackDeliveries :many
-- Each callback with what its body reports: the task's root, failure and
-- end, or the request's status, response size and end.
select c.id, c.task_id, c.request_id, c.workspace_id, c.url, c.event, c.attempt, c.max_attempts, c.failure,
       t.root_task_id, t.failure as task_failure,
       coalesce(t.finished_at, h.started_at + make_interval(secs => h.duration_ms / 1000.0)) as finished_at,
       h.status as request_status, h.response_bytes
from task_callbacks c
left join tasks t on t.id = c.task_id
left join http_requests h on h.id = c.request_id
where c.id = any(@ids::bigint[])
order by c.id;

-- name: CallbackResult :one
-- The task's result for one delivery: its data only when it is at most
-- max_bytes, so no batch holds large results.
select encoding,
       (case when octet_length(data) <= @max_bytes::int then data end)::bytea as data
from task_results
where task_id = @task_id;

-- name: FinishCallback :exec
update task_callbacks
set state = @state, last_error = sqlc.narg(last_error), finished_at = now()
where id = @id and deliveries = @deliveries and state = 'pending';

-- name: RetryCallback :exec
update task_callbacks
set next_attempt_at = now() + make_interval(secs => @delay_seconds::float8), last_error = @last_error
where id = @id and deliveries = @deliveries and state = 'pending';

-- name: PurgeFinishedCallbacks :execrows
delete from task_callbacks
where id in (
    select f.id from task_callbacks f
    where f.state <> 'pending' and f.finished_at < now() - make_interval(secs => @retain_seconds::float8)
    limit @batch_size
);

-- name: NextCallbackAt :many
-- When the earliest pending callback is due; no row without one.
select next_attempt_at from task_callbacks where state = 'pending' order by next_attempt_at limit 1;
