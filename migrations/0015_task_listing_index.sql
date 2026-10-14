-- Task listings with a status or root filter read one page per workload,
-- status and rootness.
create index tasks_listing on tasks (workload_id, status, (parent_task_id is null), id desc);
