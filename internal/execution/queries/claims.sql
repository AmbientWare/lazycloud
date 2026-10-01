-- name: AssignedContainerRelease :one
select release_id from containers where id = @id and host_id = @host_id;

-- name: LockContainerForClaim :one
-- Claims of one container run one at a time, so its slot count holds.
-- Container transitions wait for the claim to commit.
select c.state, c.host_id, c.slots, c.release_id, r.spec
from containers c
join releases r on r.id = c.release_id
where c.id = @id
for no key update of c;

-- name: CountRunningAttemptsOnContainer :one
select count(*) from attempts where container_id = @container_id and state = 'running';

-- name: ClaimQueuedTasks :many
-- Due queued tasks of the release become running attempts on the container.
-- Concurrent claimers skip each other's rows. The claim takes the longest
-- prefix whose inputs total at most max_input_bytes, and always the first
-- task, so the response stays within the host message limit.
with candidate as (
    select t.id, t.attempt_count, t.available_at
    from tasks t
    where t.release_id = @release_id and t.status = 'queued' and t.available_at <= now()
    order by t.available_at, t.id
    limit @max_tasks
    for update skip locked
), sized as (
    select c.id, c.attempt_count,
           row_number() over w as turn,
           sum(octet_length(i.data)) over w as total_bytes
    from candidate c
    join task_inputs i on i.task_id = c.id
    window w as (order by c.available_at, c.id)
), picked as (
    select id, attempt_count from sized
    where turn = 1 or total_bytes <= @max_input_bytes::bigint
), attempt as (
    insert into attempts (task_id, number, container_id, state, deadline_at)
    select picked.id, picked.attempt_count + 1, @container_id, 'running',
           now() + make_interval(secs => @timeout_seconds::float8)
    from picked
    returning attempts.id, attempts.task_id, attempts.number, attempts.deadline_at
), claimed as (
    update tasks
    set status = 'running',
        current_attempt_id = attempt.id,
        attempt_count = attempt.number,
        started_at = coalesce(tasks.started_at, now())
    from attempt
    where tasks.id = attempt.task_id
)
select attempt.task_id, attempt.id as attempt_id, attempt.number, attempt.deadline_at,
       i.encoding, i.data, t.max_attempts, t.parent_task_id,
       coalesce(t.root_task_id, t.id)::uuid as root_task_id
from attempt
join task_inputs i on i.task_id = attempt.task_id
join tasks t on t.id = attempt.task_id
order by attempt.id;

-- name: NextQueuedAt :one
-- When the release's next queued task becomes due, if any.
select available_at from tasks
where release_id = @release_id and status = 'queued'
order by available_at
limit 1;
