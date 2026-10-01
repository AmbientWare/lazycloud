-- name: LockMachineByName :one
select id, phase, state, (token_hash is not null)::bool as enrolled
from hosts
where account_id = @account_id and kind = 'machine' and name = @name and phase <> 'deleted'
for update;

-- name: MachineNameConflict :one
-- Another account's machine of this name already serves one of the
-- workspaces, so a pin would be ambiguous.
select w.name
from hosts h
join host_workspaces hw on hw.host_id = h.id
join workspaces w on w.id = hw.workspace_id
where h.kind = 'machine' and h.name = @name and h.phase <> 'deleted'
  and h.account_id is distinct from @account_id::uuid
  and hw.workspace_id = any(@workspace_ids::uuid[])
order by w.name
limit 1;

-- name: InsertMachine :one
insert into hosts (name, state, kind, provider, account_id, phase, phase_message, cpu_millis, memory_bytes, gpu_type)
values (@name, 'offline', 'machine', 'agent', @account_id, 'requested', 'Waiting for the join command to run on the host',
        0, 0, @gpu_type)
returning id;

-- name: ResetMachine :exec
-- A machine that never joined, or failed its checks, takes a new join.
update hosts
set state = 'offline', token_hash = null, phase = 'requested', failure = null,
    phase_message = 'Waiting for the join command to run on the host', phase_at = now(),
    gpu_type = @gpu_type, preflight = '[]', updated_at = now()
where id = @id;

-- name: ReplaceMachineWorkspaces :exec
with gone as (
    delete from host_workspaces where host_id = @host_id and workspace_id <> all(@workspace_ids::uuid[])
)
insert into host_workspaces (host_id, workspace_id)
select @host_id, unnest(@workspace_ids::uuid[])
on conflict do nothing;

-- name: RevokeMachineJoinTokens :exec
update host_join_tokens set used_at = now() where host_id = @host_id and used_at is null;

-- name: LockAccountMachine :one
-- A machine of the account by id or name.
select id, name, phase, (token_hash is not null)::bool as enrolled
from hosts
where account_id = @account_id and kind = 'machine'
  and (id::text = @machine::text or (name = @machine::text and phase <> 'deleted'))
order by phase = 'deleted', id desc
limit 1
for update;

-- name: PinnedDeployments :many
-- Active workloads in these workspaces whose release pins the machine name.
select distinct ws.name
from workloads wl
join apps a on a.id = wl.app_id
join workspaces ws on ws.id = a.workspace_id
join releases r on r.id = wl.active_release_id
where a.workspace_id = any(@workspace_ids::uuid[])
  and wl.desired_state <> 'deleted'
  and r.spec -> 'placement' ->> 'machine' = @name::text
order by ws.name;

-- name: RetireMachine :exec
update hosts
set state = 'retired', token_hash = null, phase = 'deleted', phase_message = @message, phase_at = now(),
    capacity_state = 'available', updated_at = now()
where id = @id;

-- name: MachineWorkspaceIDs :many
select workspace_id from host_workspaces where host_id = @host_id;

-- name: AccountMachines :many
-- The account's machines that still exist, by name after the cursor.
select h.id, h.name, h.phase, h.phase_message, h.phase_at, h.failure, h.state, h.last_seen_at,
       h.cpu_millis, h.memory_bytes, h.gpu_type, h.gpu_count, h.capacity_state, h.capacity_reason,
       h.preflight, h.agent_version, h.created_at, h.updated_at,
       array(select w.name from host_workspaces hw join workspaces w on w.id = hw.workspace_id
             where hw.host_id = h.id order by w.name)::text[] as workspaces
from hosts h
where h.account_id = @account_id and h.kind = 'machine' and h.phase <> 'deleted'
  and h.name > @after_name
order by h.name
limit @max_rows;

-- name: WorkspaceMachines :many
-- Machines that serve the workspace, by name after the cursor.
select h.id, h.name, h.phase, h.phase_message, h.phase_at, h.failure, h.state, h.last_seen_at,
       h.cpu_millis, h.memory_bytes, h.gpu_type, h.gpu_count, h.capacity_state, h.capacity_reason,
       h.preflight, h.agent_version, h.created_at, h.updated_at,
       array(select w.name from host_workspaces hw2 join workspaces w on w.id = hw2.workspace_id
             where hw2.host_id = h.id order by w.name)::text[] as workspaces
from host_workspaces hw
join hosts h on h.id = hw.host_id
where hw.workspace_id = @workspace_id and h.kind = 'machine' and h.phase <> 'deleted'
  and h.name > @after_name
order by h.name
limit @max_rows;

-- name: MachineByID :one
select h.id, h.name, h.phase, h.phase_message, h.phase_at, h.failure, h.state, h.last_seen_at,
       h.cpu_millis, h.memory_bytes, h.gpu_type, h.gpu_count, h.capacity_state, h.capacity_reason,
       h.preflight, h.agent_version, h.created_at, h.updated_at,
       array(select w.name from host_workspaces hw join workspaces w on w.id = hw.workspace_id
             where hw.host_id = h.id order by w.name)::text[] as workspaces
from hosts h
where h.id = @id;
