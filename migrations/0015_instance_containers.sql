-- Sandboxes are instance containers. Listing, searching and counting them
-- reads this index rather than every container the workspace ever ran.
create index containers_instances_recent on containers (workspace_id, id desc) where purpose = 'instance';
