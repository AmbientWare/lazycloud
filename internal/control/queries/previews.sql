-- name: EnsurePreviewWorkload :one
-- A preview of a workload never deployed creates it stopped, so it is not
-- served until a deploy. An existing workload keeps its state.
insert into workloads (app_id, kind, name, desired_state)
values (@app_id, @kind, @name, 'stopped')
on conflict (app_id, kind, name) where desired_state <> 'deleted' do update set name = excluded.name
returning id;

-- name: InsertPreviewRelease :one
insert into releases (workload_id, version, spec, spec_digest, source_sha256, traceparent)
values (@workload_id, -nextval('preview_versions'), @spec, @spec_digest, @source_sha256, sqlc.narg(traceparent)::text)
returning id, version, created_at;

-- name: InsertPreview :one
insert into previews (release_id, workspace_id, user_id, kind, lease_expires_at, deadline_at)
values (@release_id, @workspace_id, @user_id, @kind, now() + make_interval(secs => @lease_seconds::float8),
        case when @timeout_seconds::int > 0 then now() + make_interval(secs => @timeout_seconds::int) end)
returning lease_expires_at, deadline_at, created_at;

-- name: PreviewRow :one
select p.release_id, p.kind, p.lease_expires_at, p.deadline_at, p.created_at,
       (p.stopped_at is null and p.lease_expires_at > now() and (p.deadline_at is null or p.deadline_at > now()))::bool as live,
       w.name, a.name as app_name, rel.spec, rel.load_error
from previews p
join releases rel on rel.id = p.release_id
join workloads w on w.id = rel.workload_id
join apps a on a.id = w.app_id
where p.release_id = @release_id and p.workspace_id = @workspace_id;

-- name: RenewPreview :one
-- Extends a live preview's lease; a stopped or lapsed one stays stopped.
update previews
set lease_expires_at = now() + make_interval(secs => @lease_seconds::float8)
where release_id = @release_id and stopped_at is null and lease_expires_at > now()
  and (deadline_at is null or deadline_at > now())
returning lease_expires_at;

-- name: StopPreview :exec
update previews set stopped_at = coalesce(stopped_at, now())
where release_id = @release_id and workspace_id = @workspace_id;
