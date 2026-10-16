-- When the platform last asked CloudFormation to delete the authorization's
-- stack, and when it confirmed the stack deleted. Until it is confirmed,
-- removing the connection deletes the stack, and forgetting the connection
-- reports it as left in the customer's account. The stack removes its role
-- last, so once the platform asked, the role going is the stack going.
alter table cloud_authorizations
    add column stack_delete_requested_at timestamptz,
    add column stack_deleted_at timestamptz;

-- When the customer asked to remove the connection. A setup that never
-- finished waits a short while from then for a stack still being created.
alter table cloud_connections add column removal_requested_at timestamptz;
