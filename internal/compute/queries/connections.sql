-- name: ConnectionOfAccount :one
select * from cloud_connections where account_id = @account_id;

-- name: LockConnectionOfAccount :one
select * from cloud_connections where account_id = @account_id for update;

-- name: LockConnection :one
select * from cloud_connections where id = @id for update;

-- name: ConnectionAuthorizations :many
-- The connection's authorizations that hold a slot.
select * from cloud_authorizations where connection_id = @connection_id and slot is not null order by generation;

-- name: InsertConnection :one
insert into cloud_connections (account_id, aws_account_id, phase, next_step_at)
values (@account_id, @aws_account_id, 'awaiting_authorization', now())
returning *;

-- name: InsertAuthorization :one
insert into cloud_authorizations (connection_id, generation, mode, slot, phase, role_arn, external_id, region,
                                  stack_name, template_version, template_sha256, node_role_arn,
                                  node_instance_profile, networks, expires_at)
values (@connection_id, @generation, @mode, 'pending', 'awaiting_authorization', @role_arn, @external_id, @region,
        sqlc.narg(stack_name), sqlc.narg(template_version), sqlc.narg(template_sha256), sqlc.narg(node_role_arn),
        sqlc.narg(node_instance_profile), @networks, now() + interval '1 day')
returning *;

-- name: NextGeneration :one
select coalesce(max(generation), 0)::int + 1 from cloud_authorizations where connection_id = @connection_id;

-- name: SetConnectionPhase :one
-- Every phase change bumps the revision, which fences a validation that
-- started before it.
update cloud_connections
set phase = @phase, revision = revision + 1, next_step_at = sqlc.narg(next_step_at),
    step_attempts = @step_attempts, action_url = sqlc.narg(action_url), action_label = sqlc.narg(action_label),
    updated_at = now()
where id = @id
returning *;

-- name: SetAuthorizationPhase :exec
update cloud_authorizations
set phase = @phase, slot = sqlc.narg(slot), updated_at = now()
where id = @id;

-- name: StartValidation :exec
update cloud_authorizations
set phase = 'validating', last_validation_started_at = now(), updated_at = now()
where id = @id;

-- name: AuthorizationValidated :exec
update cloud_authorizations
set phase = 'ready', error_code = null, error_message = null, last_validated_at = now(),
    stack_id = coalesce(sqlc.narg(stack_id), stack_id), networks = @networks,
    node_role_arn = coalesce(sqlc.narg(node_role_arn), node_role_arn),
    node_instance_profile = coalesce(sqlc.narg(node_instance_profile), node_instance_profile),
    expires_at = null, updated_at = now()
where id = @id;

-- name: AuthorizationFailed :exec
update cloud_authorizations
set phase = 'degraded', error_code = @error_code, error_message = @error_message, updated_at = now()
where id = @id;

-- name: DeleteConnection :exec
-- The connection goes with the fleet state keyed by it: its published
-- markets and its pools' cooldowns.
with markets as (
    delete from fleet_markets where starts_with(market, sqlc.arg(id)::uuid::text || '/')
), cooldowns as (
    delete from capacity_cooldowns where connection_key = sqlc.arg(id)::uuid::text
)
delete from cloud_connections where id = sqlc.arg(id)::uuid;

-- name: ConnectionWorkspaces :many
select name from workspaces where connection_id = @connection_id order by name limit 20;

-- name: ConnectionLiveHosts :one
select count(*)::int from hosts
where connection_id = @connection_id and kind = 'connection' and phase not in ('deleted', 'failed');

-- name: ClaimDueConnections :many
-- Connections whose background step is due, each leased for
-- @lease_seconds so one scheduler replica runs a connection's step at a
-- time; the step's outcome sets when the next one is due.
update cloud_connections set next_step_at = now() + make_interval(secs => @lease_seconds::float8)
where id in (
    select id from cloud_connections where next_step_at <= now()
    order by next_step_at, id limit @batch_size
    for update skip locked
)
returning id;

-- name: RoleBoundElsewhere :one
-- Whether another connection holds an authorization for this role.
select exists (
    select 1 from cloud_authorizations a
    where a.role_arn = @role_arn and a.slot is not null and a.connection_id is distinct from sqlc.narg(connection_id)::uuid
)::bool;

-- name: AuthorizationLiveHosts :one
select count(*)::int from hosts
where authorization_id = @authorization_id and phase not in ('deleted', 'failed');

-- name: DrainAuthorizationHosts :many
-- Hosts launched under a replaced authorization stop taking work; retirement
-- terminates each once it is empty.
update hosts
set phase = 'draining', capacity_state = 'draining', capacity_reason = 'authorization replaced',
    phase_message = 'Draining; no new work is placed here', phase_at = now(), updated_at = now()
where authorization_id = @authorization_id and phase in ('ready', 'joining')
returning id;

-- name: ConnectionAccess :one
-- The connected account and the role its active authorization grants.
select cc.aws_account_id, a.region, a.role_arn, a.external_id
from cloud_connections cc
join cloud_authorizations a on a.connection_id = cc.id and a.slot = 'active'
where cc.id = @id;

-- name: RemainingStacks :many
-- The connection's stacks, of every generation, not confirmed deleted.
select id, region, stack_name::text as stack_name, stack_id from cloud_authorizations
where connection_id = @connection_id and mode = 'managed_stack' and stack_name is not null and stack_deleted_at is null
order by generation;

-- name: StackDeleted :exec
update cloud_authorizations set stack_deleted_at = now(), updated_at = now() where id = @id;

-- name: ConnectionRegions :many
-- The regions the connection's instances may run in: its networks' and
-- any its hosts record.
select h.region::text from hosts h where h.connection_id = sqlc.arg(connection)::uuid and h.region <> ''
union
select jsonb_object_keys(a.networks) from cloud_authorizations a where a.connection_id = sqlc.arg(connection)::uuid
order by 1;

-- name: ConnectionHostsAmong :many
-- Which of these hosts the connection launched.
select id from hosts where id = any(@ids::uuid[]) and connection_id = @connection_id;

-- name: LiveConnectionInstances :many
-- The instances of the connection's hosts not confirmed gone.
select instance_id::text as instance_id, region from hosts
where connection_id = @connection_id and phase not in ('deleted', 'failed') and instance_id is not null
order by instance_id;

-- name: AbandonConnectionHosts :exec
-- A forgotten connection's hosts end with it; their instances, if any
-- remain, are the customer's to remove.
update hosts
set phase = 'failed', state = 'retired', failure = 'unknown', launch_lease_until = null,
    phase_message = 'Left in the AWS account when its connection was forgotten', phase_at = now(), updated_at = now()
where connection_id = @connection_id and phase not in ('deleted', 'failed');

-- name: AccountConnectedElsewhere :one
-- Whether another connection reaches the same AWS account.
select exists (
    select 1 from cloud_connections where aws_account_id = @aws_account_id and id <> @id
)::bool;

-- name: LockWorkspaceConnection :one
-- The account's connection, share-locked so its phase holds until the
-- caller's transaction commits.
select * from cloud_connections where account_id = @account_id for share;

-- name: StackDeleteRequested :exec
update cloud_authorizations set stack_delete_requested_at = now(), updated_at = now() where id = @id;

-- name: RequestRemoval :exec
update cloud_connections set removal_requested_at = now() where id = @id;
