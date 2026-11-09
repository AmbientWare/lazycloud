-- name: InsertPreview :one
insert into previews (release_id, workspace_id, user_id, kind, lease_expires_at, deadline_at)
values (@release_id, @workspace_id, @user_id, @kind, now() + make_interval(secs => @lease_seconds::float8),
        case when @timeout_seconds::int > 0 then now() + make_interval(secs => @timeout_seconds::int) end)
returning lease_expires_at, deadline_at, created_at;

-- name: PreviewRow :one
select p.release_id, p.kind, p.lease_expires_at, p.deadline_at, p.created_at,
       preview_live(p)::bool as live,
       w.name, a.name as app_name, rel.spec, rel.load_error
from previews p
join releases rel on rel.id = p.release_id
join workloads w on w.id = rel.workload_id
join apps a on a.id = w.app_id
where p.release_id = @release_id and p.workspace_id = @workspace_id;

-- name: RenewPreview :one
-- Extends a live preview's lease; a stopped or lapsed one stays stopped.
update previews p
set lease_expires_at = now() + make_interval(secs => @lease_seconds::float8)
where p.release_id = @release_id and preview_live(p)
returning p.lease_expires_at;

-- name: StopPreview :exec
update previews set stopped_at = coalesce(stopped_at, now())
where release_id = @release_id and workspace_id = @workspace_id;
