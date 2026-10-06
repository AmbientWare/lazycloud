-- conversion: the server held the container's start until its image was
-- converted, from the container's assignment to the start sent.
alter table container_startup_stages
    drop constraint container_startup_stages_stage_check,
    add constraint container_startup_stages_stage_check
        check (stage in ('image', 'source', 'create', 'runtime', 'disk', 'conversion'));

-- A workspace's deletion hands its finished builds to another workspace
-- that resolved the same image.
create index image_builds_workspace on image_builds (workspace_id);
create index workspace_images_image on workspace_images (image_digest);

alter table image_layers drop column index_bytes, drop column data_bytes, drop column entries;

-- A trace lives as long as its reference's layer rows: the layer sweep
-- deletes it when it retires a layer of the reference.
alter table image_traces drop constraint image_traces_reference_fkey;

comment on column image_layer_uploads.container_id is
    'The build container, or the server conversion''s lease token, the upload was offered to; null for a pair the sweep retired.';
comment on column images.ready_at is 'When the image was last published.';
comment on column workspace_images.ready_at is 'When the workspace''s own build of the image was last published.';
