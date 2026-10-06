-- The image a container without one of its own runs: the server's managed
-- template for a Python version, pinned once to source, the template's
-- image by digest, and converted by the server as a platform image for each
-- host architecture (platform_images). Another template pins another
-- source.
drop table managed_images;

create table managed_images (
    python_version text not null,
    template text not null,
    source text not null,
    created_at timestamptz not null default now(),
    primary key (python_version, template)
);
