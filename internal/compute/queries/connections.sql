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
delete from cloud_connections where id = @id;

-- name: ConnectionWorkspaces :many
select name from workspaces where connection_id = @connection_id order by name limit 20;

-- name: ConnectionLiveHosts :one
select count(*)::int from hosts
where connection_id = @connection_id and kind = 'connection' and phase not in ('deleted', 'failed');

-- name: DueConnections :many
select id from cloud_connections where next_step_at <= now() order by next_step_at, id limit @batch_size;

-- name: ReadyConnectionOfAccount :one
select cc.id, cc.phase from cloud_connections cc where cc.account_id = @account_id;

-- name: ActiveAuthorizationOfConnection :one
-- The authorization the fleet launches with while the connection hosts
-- workloads or still manages its instances.
select a.*, cc.aws_account_id, cc.phase as connection_phase
from cloud_authorizations a
join cloud_connections cc on cc.id = a.connection_id
where a.connection_id = @connection_id and a.slot = 'active' and a.phase = 'ready';

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
