-- name: LockContainer :one
select id, release_id, image_build_id, state, host_id from containers where id = @id for update;

-- name: StopContainer :exec
update containers
set state = 'stopped', stop_reason = @stop_reason, exit_message = @exit_message, exit_code = sqlc.narg('exit_code'),
    stopped_at = now()
where id = @id;

-- name: RunningAttemptsOnContainer :many
select id from attempts where container_id = @container_id and state = 'running' order by id;

-- name: FailQueuedTasksOfRelease :many
-- The caller holds the rows from LockQueuedWithDependents.
update tasks
set status = 'failed', failure = @failure, finished_at = now()
where id = any(@ids::uuid[]) and status = 'queued'
returning id;

-- name: RecordLoadError :exec
update releases set load_error = @load_error where id = @id;

-- name: CountStartFailure :one
update releases set start_failures = start_failures + 1 where id = @id returning start_failures;

-- name: LockStoppedReleases :many
-- The releases among the ids that stopped starting, on a load error or
-- the start failure limit, locked, with when and why their newest serve
-- container stopped.
select r.id, r.start_failures, r.load_error, last_stop.stopped_at, coalesce(last_stop.reason, '')::text as reason
from releases r
left join lateral (
    select lc.stopped_at, coalesce(nullif(lc.exit_message, ''), lc.stop_reason)::text as reason
    from containers lc
    where lc.release_id = r.id and lc.purpose = 'serve'
    order by lc.id desc limit 1
) last_stop on true
where r.id = any(@ids::uuid[]) and (r.load_error is not null or r.start_failures >= @start_failure_limit::int)
order by r.id
for update of r;

-- name: RetryReleaseStarts :exec
update releases set start_failures = @start_failures, load_error = null where id = any(@ids::uuid[]);
