-- name: UpsertImage :one
-- The update locks the image row, so build requests for one image run one
-- at a time. An image that needs no build is ready on insert, and becomes
-- ready when a request finds it is its public base.
insert into images (digest, id, dockerfile, python_version, architecture, build_secrets, build_gpu, reference, ready_at)
values (@digest, @id, @dockerfile, @python_version, @architecture, @build_secrets, @build_gpu,
        sqlc.narg(reference), case when sqlc.narg(reference)::text is null then null else now() end)
on conflict (digest) do update
set reference = coalesce(images.reference, excluded.reference),
    ready_at = coalesce(images.ready_at, excluded.ready_at)
returning digest, id, python_version, architecture, reference, created_at, ready_at;

-- name: GrantImage :exec
insert into workspace_images (workspace_id, image_digest)
values (@workspace_id, @image_digest)
on conflict do nothing;

-- name: WorkspaceImage :one
-- The workspace's view of an image: its own rebuild, else the global one.
select i.digest, i.id, i.python_version, i.architecture,
       coalesce(w.reference, i.reference) as reference, i.reference as global_reference,
       i.created_at, coalesce(w.ready_at, i.ready_at) as ready_at
from images i
join workspace_images w on w.image_digest = i.digest
where w.workspace_id = @workspace_id and i.id = @id;

-- name: WorkspaceOnCustomerHosts :one
-- A workspace bound to a connected AWS account runs its builds there.
select (connection_id is not null)::bool from workspaces where id = @id;

-- name: HostKind :one
select kind from hosts where id = @id;

-- name: LockImage :one
select digest, id, python_version, architecture, reference, created_at, ready_at
from images where digest = @digest for update;

-- name: ActiveBuild :one
-- The build a request joins: the global one, or the workspace's own
-- workspace-scoped build (forced = true).
select id, image_digest, state, failure, created_at, finished_at
from image_builds
where image_digest = @image_digest and state = 'building'
  and forced = @forced and (not @forced or workspace_id = @workspace_id);

-- name: LatestBuild :one
select id, image_digest, state, failure, created_at, finished_at
from image_builds where image_digest = @image_digest
order by created_at desc, id desc limit 1;

-- name: InsertBuild :one
insert into image_builds (image_digest, state, workspace_id, forced, context_sha256, registry_auth, deadline_at)
values (@image_digest, 'building', @workspace_id, @forced, sqlc.narg(context_sha256), sqlc.narg(registry_auth),
        now() + make_interval(secs => @timeout_seconds::float8))
returning id, image_digest, state, failure, created_at, finished_at;

-- name: WorkspaceBuild :one
-- A build of an image the workspace resolved.
select b.id, b.image_digest, b.state, b.failure, b.created_at, b.finished_at, i.id as image_id
from image_builds b
join images i on i.digest = b.image_digest
join workspace_images w on w.image_digest = b.image_digest and w.workspace_id = @workspace_id
where b.id = @id;

-- name: BuildPhase :one
-- The state of the build's newest container and how many it had.
select coalesce((select c.state from containers c where c.image_build_id = @id order by c.created_at desc, c.id desc limit 1), '')::text as container_state,
       (select count(*) from containers c where c.image_build_id = @id)::int as attempts;

-- name: BuildToStart :one
select b.id, b.state, b.workspace_id, b.forced, b.context_sha256, b.registry_auth, b.deadline_at,
       i.digest, i.dockerfile, i.architecture, i.build_secrets, i.build_gpu
from image_builds b
join images i on i.digest = b.image_digest
where b.id = @id;

-- name: LockBuild :one
select id, image_digest, state, workspace_id, forced, deadline_at, log_bytes, log_lines
from image_builds where id = @id for update;

-- name: SucceedBuild :exec
update image_builds
set state = 'succeeded', registry_auth = null, finished_at = now()
where id = @id;

-- name: FailBuild :exec
update image_builds
set state = 'failed', failure = @failure::text, registry_auth = null, finished_at = now()
where id = @id;

-- name: PublishWorkspaceImage :exec
update workspace_images set reference = @reference::text, ready_at = now()
where workspace_id = @workspace_id and image_digest = @image_digest;

-- name: CountBuildLogs :exec
update image_builds set log_bytes = log_bytes + @bytes, log_lines = log_lines + @lines where id = @id;

-- name: ResetBuildLogCount :exec
update image_builds set log_bytes = 0, log_lines = 0 where id = @id;

-- name: PublishImage :exec
update images set reference = @reference, ready_at = now() where digest = @digest;

-- name: BuildsToRecover :many
-- Building builds past their deadline or whose newest container stopped.
-- Reads the image_builds_building partial index.
select b.id, b.image_digest
from image_builds b
where b.state = 'building'
  and (b.deadline_at < now()
       or coalesce((select c.state from containers c where c.image_build_id = b.id
                    order by c.created_at desc, c.id desc limit 1), 'stopped') = 'stopped')
  and b.id > @after_id
order by b.id
limit @batch_size;

-- name: InsertBuildLogs :exec
insert into image_build_logs (build_id, attempt, data, logged_at)
select @build_id, @attempt, unnest(@data::text[]), unnest(@logged_at::timestamptz[]);

-- name: BuildLogsAfter :many
select id, attempt, data, logged_at
from image_build_logs
where build_id = @build_id and id > @after
order by id
limit @max_entries;

-- name: BuildImageDigest :one
select image_digest from image_builds where id = @id;

-- name: ImageBuildGPU :one
select build_gpu from images where digest = @digest;

-- name: SourceRegistered :one
select exists (select 1 from source_objects where workspace_id = @workspace_id and sha256 = @sha256)::bool;

-- name: ImageArchitecture :one
select architecture from images where id = @id;
