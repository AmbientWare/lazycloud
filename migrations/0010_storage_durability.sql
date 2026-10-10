-- Why the holder's last publish or release of a disk failed. A later
-- generation, a release or a new holder clears it.
alter table disks
    add column failed_operation text check (failed_operation in ('publish', 'release')),
    add column failure_message text,
    add column failed_at timestamptz,
    add check ((failed_operation is null) = (failure_message is null) and (failed_operation is null) = (failed_at is null));
