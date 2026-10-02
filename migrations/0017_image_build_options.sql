-- Build secrets and GPU builds. build_secrets maps each workspace secret the
-- build reads to the version the definition resolved; with the workspace it
-- is part of the image identity, so an image built with one workspace's
-- secrets is never another's. The values are read when the build starts and
-- reach only the builder's secret mount. build_gpu is the GPU model the
-- build container runs on.
alter table images
    add column build_secrets jsonb not null default '{}',
    add column build_gpu text not null default '';

-- The GPU model a build container needs, in the form of a release's gpu
-- list, or null for a CPU build or a workload container. Placement and
-- capacity read it where they read a release's.
create function build_gpus(build uuid) returns jsonb
language sql stable
return case when build is null then null else (
    select jsonb_build_array(i.build_gpu)
    from image_builds b
    join images i on i.digest = b.image_digest
    where b.id = build and i.build_gpu <> ''
) end;
