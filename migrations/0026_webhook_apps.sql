-- A webhook takes the events of the apps it names, by name, or of every app
-- when it names none.
alter table webhooks add column apps text[] not null default '{}'
    check (cardinality(apps) <= 50 and array_position(apps, null) is null);
update webhooks set apps = array[app_name] where app_name is not null;
alter table webhooks drop column app_name;

create or replace function lc_webhook_event(event_workspace uuid, event_app text, event_type text, event_data jsonb) returns integer
language plpgsql as $$
declare
    shared_id uuid := uuidv7();
    written integer;
begin
    insert into webhook_deliveries (webhook_id, workspace_id, event_id, event, data)
    select h.id, h.workspace_id, shared_id, event_type, event_data
    from webhooks h
    where h.workspace_id = event_workspace and h.enabled and event_type = any(h.events)
      and (cardinality(h.apps) = 0 or event_app = any(h.apps));
    get diagnostics written = row_count;
    if written > 0 then
        perform pg_notify('lc_callback', '');
    end if;
    return written;
end
$$;
