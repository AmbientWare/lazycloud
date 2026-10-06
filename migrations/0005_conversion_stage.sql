-- conversion: the server held the container's start until its image was
-- converted, from the first sync that derived the start to the start sent.
alter table container_startup_stages
    drop constraint container_startup_stages_stage_check,
    add constraint container_startup_stages_stage_check
        check (stage in ('image', 'source', 'create', 'runtime', 'disk', 'conversion'));
