-- name: LockSnapshotSource :one
-- A container that can be snapshotted: ready, on a host, in the workspace.
select c.id, c.host_id, c.release_id::uuid as release_id, c.state, c.purpose, w.kind, r.spec
from containers c
join releases r on r.id = c.release_id
join workloads w on w.id = r.workload_id
where c.id = @id and c.workspace_id = @workspace_id
for share of c;

-- name: InsertSnapshot :one
insert into memory_snapshots (id, workspace_id, release_id, container_id, automatic, state)
values (coalesce(sqlc.narg('id')::uuid, uuidv7()), @workspace_id, @release_id, @container_id, @automatic, 'pending')
returning id, created_at;

-- name: SnapshotView :one
select id, container_id, release_id, workspace_id, automatic, state, failure, size_bytes, sha256, created_at, finished_at
from memory_snapshots where id = @id;

-- name: PendingSnapshotsOnHost :many
-- Snapshots the host's live containers still owe, with how a pod's
-- automatic one waits for readiness.
select s.id, s.workspace_id, s.container_id::uuid as container_id, s.automatic, s.created_at, r.spec
from memory_snapshots s
join containers c on c.id = s.container_id
join releases r on r.id = s.release_id
where c.host_id = @host_id and c.state in ('ready', 'draining') and s.state = 'pending'
order by s.id;

-- name: FinishSnapshot :one
-- The host holding the snapshot's container reports its outcome once.
update memory_snapshots s
set state = @state, failure = sqlc.narg('failure'), size_bytes = sqlc.narg('size_bytes'),
    sha256 = sqlc.narg('sha256'), finished_at = now()
from containers c
where s.id = @id and s.state = 'pending' and c.id = s.container_id and c.id = @container_id and c.host_id = @host_id
returning s.id;

-- name: FailStaleSnapshots :many
-- Pending snapshots whose container stopped or that ran past the deadline.
update memory_snapshots s
set state = 'failed', failure = 'the container stopped or the snapshot timed out', finished_at = now()
where s.id in (
    select p.id from memory_snapshots p
    left join containers c on c.id = p.container_id
    where p.state = 'pending'
      and (c.id is null or c.state = 'stopped' or p.created_at < now() - make_interval(secs => @deadline_seconds::float8))
    order by p.id
    limit 100
    for update of p skip locked
)
returning s.id;

-- name: AutomaticSnapshotCandidates :many
-- Ready serve containers of checkpoint-enabled releases that have no
-- automatic snapshot pending or available: the oldest per release takes one.
select distinct on (c.release_id) c.id, c.workspace_id, c.release_id::uuid as release_id
from containers c
join releases r on r.id = c.release_id
where c.state = 'ready' and c.purpose = 'serve' and r.spec ? 'checkpoint'
  and not exists (
      select 1 from memory_snapshots s where s.release_id = c.release_id and s.automatic and s.state <> 'failed'
  )
order by c.release_id, c.ready_at, c.id
limit 100;

-- name: InsertAutomaticSnapshot :execrows
-- One automatic snapshot per release at a time; a concurrent insert loses.
insert into memory_snapshots (workspace_id, release_id, container_id, automatic, state)
values (@workspace_id, @release_id, @container_id, true, 'pending')
on conflict (release_id) where automatic and state <> 'failed' do nothing;

-- name: RestoreSnapshot :one
-- What a starting container restores: its own snapshot, or the available
-- automatic snapshot of its release.
select s.id, s.sha256::text as sha256, s.automatic, s.workspace_id
from containers c
join memory_snapshots s on s.id = c.snapshot_id
    or (c.snapshot_id is null and c.purpose = 'serve' and s.release_id = c.release_id and s.automatic)
where c.id = @container_id and s.state = 'available'
order by (s.id = c.snapshot_id) desc, s.created_at desc
limit 1;

-- name: RecordRestoreFailed :exec
-- An automatic snapshot that cannot restore is not offered again.
update memory_snapshots set state = 'failed', failure = 'restore failed: ' || @reason::text
where id = @id and automatic and state = 'available';

-- name: SnapshotOfWorkspace :one
select s.id, s.release_id, s.state from memory_snapshots s where s.id = @id and s.workspace_id = @workspace_id;

-- name: InsertFilesystemImage :one
insert into filesystem_images (workspace_id, container_id, state) values (@workspace_id, @container_id, 'publishing')
returning id;

-- name: PendingFilesystemImagesOnHost :many
select f.id, f.container_id, f.workspace_id, f.created_at
from filesystem_images f
join containers c on c.id = f.container_id
where c.host_id = @host_id and c.state in ('ready', 'draining') and f.state = 'publishing'
order by f.id;

-- name: FinishFilesystemImage :one
update filesystem_images f
set state = @state, image_id = sqlc.narg('image_id'), failure = sqlc.narg('failure'), finished_at = now()
from containers c
where f.id = @id and f.state = 'publishing' and c.id = f.container_id and c.id = @container_id and c.host_id = @host_id
returning f.id, f.workspace_id;

-- name: FilesystemImageView :one
select id, state, image_id, failure from filesystem_images where id = @id;

-- name: FailStaleFilesystemImages :many
update filesystem_images f
set state = 'failed', failure = 'the container stopped or publishing timed out', finished_at = now()
where f.id in (
    select p.id from filesystem_images p
    join containers c on c.id = p.container_id
    where p.state = 'publishing'
      and (c.state = 'stopped' or p.created_at < now() - make_interval(secs => @deadline_seconds::float8))
    order by p.id
    limit 100
    for update of p skip locked
)
returning f.id;
