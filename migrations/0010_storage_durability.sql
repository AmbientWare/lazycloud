-- Why the holder's last publish or release of a disk failed. A later
-- generation, a release or a new holder clears it.
alter table disks
    add column failed_operation text check (failed_operation in ('publish', 'release')),
    add column failure_message text,
    add column failed_at timestamptz,
    add check ((failed_operation is null) = (failure_message is null) and (failed_operation is null) = (failed_at is null));

-- The change stream's storage.disks topic: a disk is created, deleted, or
-- its holder's failure is recorded or cleared. Publishing a generation
-- alone changes nothing users see.
create function observe_disks() returns trigger
language plpgsql as $$
begin
    if tg_op = 'INSERT' then
        perform publish_changes(c.workspace_id, c.items) from (
            select n.workspace_id, jsonb_agg(jsonb_build_object(
                'topic', 'storage.disks', 'change', 'created', 'resource_id', n.id)) as items
            from new_rows n group by n.workspace_id
        ) c;
    else
        perform publish_changes(c.workspace_id, c.items) from (
            select n.workspace_id, jsonb_agg(jsonb_build_object(
                'topic', 'storage.disks',
                'change', case when n.state = 'deleting' then 'deleted' else 'updated' end,
                'resource_id', n.id)) as items
            from new_rows n
            join old_rows o on o.id = n.id
            where o.state <> n.state or o.failed_at is distinct from n.failed_at
            group by n.workspace_id
        ) c;
    end if;
    return null;
end $$;

create trigger disks_created after insert on disks
    referencing new table as new_rows for each statement execute function observe_disks();
create trigger disks_updated after update on disks
    referencing old table as old_rows new table as new_rows for each statement execute function observe_disks();

-- Storage creates every workspace's bucket again on next use; the volumes
-- and disks in the current ones go with them.
delete from volumes where workspace_id in (select workspace_id from workspace_buckets);
delete from disks where workspace_id in (select workspace_id from workspace_buckets);
delete from storage_grants where workspace_id in (select workspace_id from workspace_buckets);
delete from workspace_buckets;

-- A workspace's bucket is in the account its workspace lives in, and
-- region is where the bucket was created. Storage deletes the bucket and
-- this row before its workspace goes, and a connection cannot go while a
-- workspace lives in it.
alter table workspace_buckets
    add column region text not null,
    drop constraint workspace_buckets_workspace_id_fkey,
    add foreign key (workspace_id) references workspaces (id);

-- Each disk generation's index names every frame of the disk, so no
-- generation builds on another.
alter table disk_generations
    drop column parent_generation,
    drop column flat;

-- The disks a host holds at once, as its agent offers them: its data
-- volume keeps room for each one's unpublished writes.
alter table hosts add column disk_slots int not null default 0 check (disk_slots >= 0);

-- The volume mounters a release's container runs: one for all its platform
-- volumes and one per distinct cloud bucket, whatever the prefix.
create function release_mounters(spec jsonb) returns integer
language sql immutable
return (
    select count(distinct coalesce((v -> 'cloud_bucket') - 'prefix', 'null'))
    from jsonb_array_elements(coalesce(spec -> 'volumes', '[]'::jsonb)) v
);
