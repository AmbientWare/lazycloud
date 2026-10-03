-- name: ClaimDueCallbacks :many
-- Leases due callbacks to this deliverer in per-workspace round robin:
-- every workspace's oldest due callback, then every workspace's second, so
-- one workspace's backlog never holds back another's. The workspaces with
-- due callbacks come from a skip scan of task_callbacks_due_workspace.
-- next_attempt_at moves past the delivery timeout and deliveries counts the
-- attempt, which fences a late outcome from an earlier lease.
with recursive spaces as (
    (select d.workspace_id from task_callbacks d
     where d.state = 'pending' and d.next_attempt_at <= now()
     order by d.workspace_id
     limit 1)
    union all
    select (select d.workspace_id from task_callbacks d
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
        from task_callbacks d
        where d.workspace_id = s.workspace_id and d.state = 'pending' and d.next_attempt_at <= now()
        order by d.next_attempt_at, d.id
        limit @batch_size
    ) q
    where s.workspace_id is not null
), picked as (
    select p.id from task_callbacks p
    where p.id in (select t.id from turns t order by t.turn, t.next_attempt_at, t.id limit @batch_size)
      and p.state = 'pending' and p.next_attempt_at <= now()
    for update skip locked
)
update task_callbacks c
set next_attempt_at = now() + make_interval(secs => @lease_seconds::float8),
    deliveries = c.deliveries + 1
from picked
where c.id = picked.id
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
