-- name: LockContainer :one
select id, release_id, state, host_id from containers where id = @id for update;

-- name: StopContainer :exec
update containers
set state = 'stopped', stop_reason = @stop_reason, exit_message = @exit_message, stopped_at = now()
where id = @id;

-- name: RunningAttemptsOnContainer :many
select id from attempts where container_id = @container_id and state = 'running' order by id;

-- name: FailQueuedTasksOfRelease :many
-- Locks in id order, as dependency resolution does.
update tasks
set status = 'failed', failure = @failure, finished_at = now()
where id in (
    select q.id from tasks q
    where q.release_id = @release_id and q.status = 'queued'
    order by q.id
    for update
)
  and status = 'queued'
returning id;

-- name: CountStartFailure :one
update releases set start_failures = start_failures + 1 where id = @id returning start_failures;
