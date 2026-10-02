-- name: AssignedContainerRelease :one
select release_id::uuid as release_id from containers where id = @id and host_id = @host_id and release_id is not null;

-- name: LockContainerForClaim :one
-- Claims of one container run one at a time, so its slot count holds.
-- Container transitions wait for the claim to commit.
select c.state, c.host_id, c.slots, c.purpose, c.release_id::uuid as release_id, r.spec
from containers c
join releases r on r.id = c.release_id
where c.id = @id
for no key update of c;

-- name: CountRunningAttemptsOnContainer :one
select count(*) from attempts where container_id = @container_id and state = 'running';

-- name: LockClaimCandidates :many
-- Due queued tasks of the release, oldest first, with the bytes each hands
-- its runner: its input and its upstream results. Concurrent claimers skip
-- each other's rows, and the locks hold until the claim commits.
select t.id, (octet_length(i.data) + coalesce(dep.bytes, 0))::bigint as input_bytes
from (
    select t.id, t.available_at
    from tasks t
    where t.release_id = @release_id and t.status = 'queued' and t.available_at <= now()
      and t.unmet_dependencies = 0
    order by t.available_at, t.id
    limit @max_tasks
    for update skip locked
) t
join task_inputs i on i.task_id = t.id
cross join lateral (
    select sum(octet_length(r.data)) as bytes
    from task_dependencies d
    join task_results r on r.task_id = d.depends_on
    where d.task_id = t.id
) dep
order by t.available_at, t.id;

-- name: InsertClaimAttempts :exec
-- A running attempt on the container for each locked task, in claim order.
insert into attempts (task_id, number, container_id, state, deadline_at)
select t.id, t.attempt_count + 1, @container_id, 'running',
       now() + make_interval(secs => @timeout_seconds::float8)
from unnest(@task_ids::uuid[]) with ordinality as p(id, n)
join tasks t on t.id = p.id
order by p.n;

-- name: StartClaimedTasks :many
-- Runs after InsertClaimAttempts: each task moves to the attempt it just got.
update tasks t
set status = 'running',
    current_attempt_id = a.id,
    attempt_count = a.number,
    started_at = coalesce(t.started_at, now())
from attempts a
join task_inputs i on i.task_id = a.task_id
where t.id = any(@task_ids::uuid[]) and a.task_id = t.id and a.number = t.attempt_count + 1
returning t.id as task_id, a.id as attempt_id, a.number, a.deadline_at,
          i.encoding, i.data, t.max_attempts, t.parent_task_id,
          coalesce(t.root_task_id, t.id)::uuid as root_task_id, t.traceparent;

-- name: NextQueuedAt :one
-- When the release's next queued task becomes due, if any.
select available_at from tasks
where release_id = @release_id and status = 'queued' and unmet_dependencies = 0
order by available_at
limit 1;
