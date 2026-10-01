-- name: EnsureWorkload :one
-- The live workload, created without a release when missing. Unlike a
-- deploy it never reactivates a stopped workload. The no-op update locks the
-- row, so concurrent prepares of one definition insert one release.
insert into workloads (app_id, kind, name, desired_state)
values (@app_id, 'function', @name, 'active')
on conflict (app_id, kind, name) where desired_state <> 'deleted' do update set name = excluded.name
returning id, active_release_id, desired_state;

-- name: ReleaseByDigest :one
-- The active release when it matches, else the newest matching one. With
-- unversioned_only it matches working-tree releases alone.
select r.id, r.version, r.created_at
from releases r
where r.workload_id = @workload_id and r.spec_digest = @spec_digest
  and (not @unversioned_only::bool or r.version is null)
order by (r.id = sqlc.narg(active_release_id)::uuid) desc nulls last, r.id desc
limit 1;
