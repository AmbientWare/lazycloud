-- name: UpsertApp :one
-- The update locks the app row, so deploys of one app run one at a time.
insert into apps (workspace_id, name, state)
values (@workspace_id, @name, 'active')
on conflict (workspace_id, name) do update set name = excluded.name
returning id, name, state, created_at;

-- name: RegisteredSources :many
select sha256 from source_objects
where workspace_id = @workspace_id and sha256 = any(@digests::bytea[]);

-- name: UpsertWorkload :one
-- Locks the workload row for the release switch.
insert into workloads (app_id, kind, name, desired_state)
values (@app_id, @kind, @name, 'active')
on conflict (app_id, kind, name) do update set desired_state = 'active'
returning id, active_release_id, next_version;

-- name: ActiveRelease :one
select id, version, spec_digest, created_at from releases where id = @id;

-- name: InsertRelease :one
insert into releases (workload_id, version, spec, spec_digest, source_sha256)
values (@workload_id, @version, @spec, @spec_digest, @source_sha256)
returning id, version, created_at;

-- name: ActivateRelease :exec
update workloads set active_release_id = @release_id::uuid, next_version = next_version + 1 where id = @id;

-- name: PruneFunctions :many
update workloads
set desired_state = 'stopped'
where app_id = @app_id and desired_state = 'active' and not (kind || ':' || name = any(@keep::text[]))
returning name, active_release_id;

-- name: FunctionRelease :one
select w.name, w.desired_state, a.name as app_name,
       r.id, r.version, r.spec, r.created_at
from workloads w
join apps a on a.id = w.app_id
join releases r on r.id = w.active_release_id
where a.workspace_id = @workspace_id and a.name = @app_name and w.kind = 'function' and w.name = @name;
