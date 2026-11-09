-- name: ReleaseByDigest :one
-- The active release when it matches, else the newest matching one. With
-- unversioned_only it matches working-tree releases alone.
select r.id, r.version, r.created_at
from releases r
where r.workload_id = @workload_id and r.spec_digest = @spec_digest
  and (not @unversioned_only::bool or r.version is null)
order by (r.id = sqlc.narg(active_release_id)::uuid) desc nulls last, r.id desc
limit 1;
