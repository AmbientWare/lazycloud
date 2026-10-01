-- name: PublishTaskStates :exec
-- One notification per workspace with the current status of each task,
-- in a transaction of its own.
select publish_changes(c.workspace_id, c.items) from (
    select t.workspace_id, jsonb_agg(jsonb_strip_nulls(jsonb_build_object(
        'topic', 'tasks', 'change', 'updated', 'resource_id', t.id, 'task_id', t.id,
        'root_task_id', t.root_task_id, 'app_id', w.app_id, 'deployment_id', t.workload_id,
        'status', t.status))) as items
    from tasks t
    join workloads w on w.id = t.workload_id
    where t.id = any(@ids::uuid[])
    group by t.workspace_id
) c;
