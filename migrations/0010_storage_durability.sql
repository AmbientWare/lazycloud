-- Why the holder's last publish or release of a disk failed. A later
-- generation, a release or a new holder clears it.
alter table disks
    add column failed_operation text check (failed_operation in ('publish', 'release')),
    add column failure_message text,
    add column failed_at timestamptz,
    add check ((failed_operation is null) = (failure_message is null) and (failed_operation is null) = (failed_at is null));

-- A workspace's bucket is in the platform account (null connection_id) or
-- in the connected account its workspace lives in, and region is where the
-- bucket was created. Storage deletes the bucket and this row before its
-- workspace goes, and the connection cannot go while the row exists.
alter table workspace_buckets
    add column connection_id uuid references cloud_connections (id),
    add column region text;
-- Every bucket made before regions were recorded is the platform's, in
-- us-east-2.
update workspace_buckets set region = 'us-east-2';
alter table workspace_buckets
    alter column region set not null,
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
