-- Why the holder's last publish or release of a disk failed. A later
-- generation, a release or a new holder clears it.
alter table disks
    add column failed_operation text check (failed_operation in ('publish', 'release')),
    add column failure_message text,
    add column failed_at timestamptz,
    add check ((failed_operation is null) = (failure_message is null) and (failed_operation is null) = (failed_at is null));

-- Storage creates every workspace's bucket again on next use; the volumes
-- and disks in the current ones go with them.
delete from volumes where workspace_id in (select workspace_id from workspace_buckets);
delete from disks where workspace_id in (select workspace_id from workspace_buckets);
delete from storage_grants where workspace_id in (select workspace_id from workspace_buckets);
delete from workspace_buckets;

-- A workspace's bucket is in the platform account (null connection_id) or
-- in the connected account its workspace lives in, and region is where the
-- bucket was created. Storage deletes the bucket and this row before its
-- workspace goes, and the connection cannot go while the row exists.
alter table workspace_buckets
    add column connection_id uuid references cloud_connections (id),
    add column region text not null,
    drop constraint workspace_buckets_workspace_id_fkey,
    add foreign key (workspace_id) references workspaces (id);
