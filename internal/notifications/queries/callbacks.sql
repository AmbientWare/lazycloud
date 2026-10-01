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
select c.id, c.task_id, c.workspace_id, c.url, c.event, c.attempt, c.max_attempts, c.failure,
       coalesce(t.root_task_id, t.id)::uuid as root_task_id, t.failure as task_failure, t.finished_at,
       r.encoding as result_encoding, r.data as result_data
from task_callbacks c
join tasks t on t.id = c.task_id
left join task_results r on r.task_id = c.task_id and c.event = 'succeeded'
where c.id = any(@ids::bigint[])
order by c.id;

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
