-- Volumes and usage publish on the change stream too, so the dashboard's
-- volume list and usage page follow them as the reference's did.

-- A volume is created, measured (size_bytes) or deleted; a deleting volume
-- is gone for its users.
create function observe_created_volumes() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'storage.volumes', 'change', 'created', 'resource_id', n.id)) as items
        from new_rows n group by n.workspace_id
    ) c;
    return null;
end $$;

create function observe_updated_volumes() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'storage.volumes',
            'change', case when n.state = 'deleting' then 'deleted' else 'updated' end,
            'resource_id', n.id)) as items
        from new_rows n
        join old_rows o on o.id = n.id
        where o.state <> n.state or o.size_bytes <> n.size_bytes
        group by n.workspace_id
    ) c;
    return null;
end $$;

create function observe_deleted_volumes() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select o.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'storage.volumes', 'change', 'deleted', 'resource_id', o.id)) as items
        from old_rows o group by o.workspace_id
    ) c;
    return null;
end $$;

create trigger volumes_created after insert on volumes
    referencing new table as new_rows for each statement execute function observe_created_volumes();
create trigger volumes_updated after update on volumes
    referencing old table as old_rows new table as new_rows for each statement execute function observe_updated_volumes();
create trigger volumes_deleted after delete on volumes
    referencing old table as old_rows for each statement execute function observe_deleted_volumes();

-- Metering writes ledger entries in batches; each statement tells every
-- workspace it charged once that its usage moved.
create function observe_ledger_entries() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select n.workspace_id, jsonb_build_array(jsonb_build_object(
            'topic', 'usage', 'change', 'updated', 'count', count(*))) as items
        from new_rows n group by n.workspace_id
    ) c;
    return null;
end $$;

create trigger ledger_entries_created after insert on ledger_entries
    referencing new table as new_rows for each statement execute function observe_ledger_entries();
