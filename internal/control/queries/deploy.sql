-- name: UpsertApp :one
-- The update locks the live app row, so deploys of one app run one at a time.
insert into apps (workspace_id, name, state)
values (@workspace_id, @name, 'active')
on conflict (workspace_id, name) where state <> 'deleted' do update set name = excluded.name
returning id, name, state, created_at;

-- name: RegisteredSources :many
select sha256 from source_objects
where workspace_id = @workspace_id and sha256 = any(@digests::bytea[]);

-- name: UpsertWorkload :one
-- Locks the live workload row for the release switch.
insert into workloads (app_id, kind, name, desired_state)
values (@app_id, 'function', @name, 'active')
on conflict (app_id, kind, name) where desired_state <> 'deleted' do update set desired_state = 'active'
returning id, active_release_id, next_version;

-- name: ActiveRelease :one
select id, version, spec_digest, created_at from releases where id = @id;

-- name: InsertRelease :one
insert into releases (workload_id, version, spec, spec_digest, source_sha256)
values (@workload_id, sqlc.narg(version), @spec, @spec_digest, @source_sha256)
returning id, version, created_at;

-- name: ActivateRelease :exec
update workloads set active_release_id = @release_id::uuid, next_version = next_version + 1 where id = @id;

-- name: PruneFunctions :many
-- Deletes the app's deployed functions that the deploy does not list.
update workloads w
set desired_state = 'deleted', deleted_at = now()
where w.app_id = @app_id and w.kind = 'function' and w.desired_state <> 'deleted'
  and w.active_release_id is not null
  and not (w.name = any(@keep::text[]))
returning w.name,
          (select count(*) from releases r where r.workload_id = w.id and r.version is not null)::int as versions;

-- name: CountDeployedWorkloads :one
select count(*)::int from workloads
where app_id = @app_id and desired_state <> 'deleted' and active_release_id is not null;

-- name: FunctionRelease :one
select w.name, w.desired_state, a.name as app_name,
       r.id, r.version, r.spec, r.created_at
from workloads w
join apps a on a.id = w.app_id
join releases r on r.id = w.active_release_id
where a.workspace_id = @workspace_id and a.name = @app_name and a.state <> 'deleted'
  and w.kind = 'function' and w.name = @name and w.desired_state <> 'deleted';
