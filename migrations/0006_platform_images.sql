-- The images agents run on their own (the image builder, the volume mount
-- image), converted by the server for one host architecture. reference is
-- the public image by digest an agent names; mirror is its copy for that
-- architecture in the platform registry, whose converted layers are rows of
-- image_reference_layers like any reference's.
--
-- A conversion holds the lease (lease_token until leased_until), so one
-- server replica converts a reference at a time; the record of its result
-- names the token, so an owner whose lease lapsed records nothing. failure
-- is the last attempt's, and failure_transient marks one that says nothing
-- of the image, such as an unreachable registry or store.
create table platform_images (
    reference text not null,
    architecture text not null check (architecture in ('amd64', 'arm64')),
    mirror text,
    lease_token uuid,
    leased_until timestamptz,
    failure text,
    failure_transient boolean not null default false,
    failed_at timestamptz,
    created_at timestamptz not null default now(),
    primary key (reference, architecture)
);
