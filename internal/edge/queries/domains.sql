-- name: DomainByHostname :one
select * from custom_domains where user_id = @user_id and hostname = @hostname;

-- name: InsertDomain :one
insert into custom_domains (user_id, hostname, phase, provider_hostname_id, required_records, error_code, error_message, last_checked_at)
values (@user_id, @hostname, @phase, @provider_hostname_id, @required_records, sqlc.narg(error_code), sqlc.narg(error_message), now())
returning *;

-- name: ListDomains :many
select * from custom_domains
where user_id = @user_id and hostname > @after
order by hostname
limit @max_rows;

-- name: SettleDomain :one
-- verified_at is stamped once, when the domain first serves.
update custom_domains
set phase = @phase,
    required_records = @required_records,
    error_code = sqlc.narg(error_code),
    error_message = sqlc.narg(error_message),
    verified_at = case when @phase = 'ready' then coalesce(verified_at, now()) else verified_at end,
    last_checked_at = now(),
    updated_at = now()
where id = @id
returning *;

-- name: DeleteDomain :exec
delete from custom_domains where id = @id;

-- name: DomainServes :many
-- Deployments that claim the hostname, in any workspace; visible says
-- whether the user owns the deployment's workspace.
select a.name as app_name, w.name,
       exists (
           select 1 from workspace_members m
           where m.workspace_id = a.workspace_id and m.user_id = @user_id and m.role = 'owner'
       )::bool as visible
from http_routes r
join workloads w on w.id = r.workload_id
join apps a on a.id = w.app_id
where r.hostname = @hostname and w.desired_state <> 'deleted' and a.state <> 'deleted'
order by a.name, w.name;

-- name: UnsettledDomains :many
select * from custom_domains
where phase in ('awaiting_verification', 'validating') and (last_checked_at is null or last_checked_at < @before)
order by last_checked_at nulls first, id
limit @max_rows;
