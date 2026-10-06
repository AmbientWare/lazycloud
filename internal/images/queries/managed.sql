-- name: ManagedSource :one
-- The source pinned for a Python version and template.
select source from managed_images where python_version = @python_version and template = @template;

-- name: RecordManagedSource :one
-- The first source recorded for a version and template stays.
with recorded as (
    insert into managed_images (python_version, template, source)
    values (@python_version, @template, @source)
    on conflict do nothing
    returning source
)
select source from recorded
union all
select source from managed_images where python_version = @python_version and template = @template
limit 1;

-- name: HostArchitecture :one
select architecture from hosts where id = @id;

-- name: ImageDigestOf :one
select digest from images where id = @id;

-- name: ImageRuntime :one
select python_version, architecture from images where id = @id;
