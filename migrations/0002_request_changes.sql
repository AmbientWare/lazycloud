-- The edge writes request records in batches. Each statement publishes one
-- change per deployment, with its request count.
create function observe_created_requests() returns trigger
language plpgsql as $$
begin
    perform publish_changes(c.workspace_id, c.items) from (
        select g.workspace_id, jsonb_agg(jsonb_build_object(
            'topic', 'requests', 'change', 'created', 'app_id', w.app_id, 'deployment_id', g.workload_id,
            'count', g.requests)) as items
        from (select workspace_id, workload_id, count(*) as requests from new_rows group by 1, 2) g
        join workloads w on w.id = g.workload_id
        group by g.workspace_id
    ) c;
    return null;
end $$;

create trigger http_requests_created after insert on http_requests
    referencing new table as new_rows for each statement execute function observe_created_requests();
