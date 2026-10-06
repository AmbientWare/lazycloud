-- name: WorkspaceOwner :one
select user_id from workspace_members where workspace_id = @workspace_id and role = 'owner';

-- name: AdmissionWorkspace :one
-- The workspace's owner, and whether it lives in a connected account, whose
-- capacity serves its containers instead of the platform fleet.
select m.user_id, (w.connection_id is not null)::bool as connected
from workspace_members m
join workspaces w on w.id = m.workspace_id
where m.workspace_id = @workspace_id and m.role = 'owner';

-- name: AccountStanding :one
select a.terms_version, a.scheduled_terms_version, a.status, a.payment_method_attached_at, a.complimentary_since,
       a.monthly_usage_limit_nanos, b.balance_nanos, b.accrued_nanos, b.month_spent_nanos, b.month_started_at,
       exists (select 1 from plan_changes p where p.user_id = a.user_id and p.state = 'open')::bool as plan_change_pending,
       now()::timestamptz as now
from billing_accounts a
join billing_balances b on b.user_id = a.user_id
where a.user_id = @user_id;

-- name: LockAccountContainers :exec
-- Serializes the account's plan-limited additions, so containers counted
-- across planning and image builds, and workspaces and members, are exact.
select pg_advisory_xact_lock(hashtextextended('billing-account:' || cast(@user_id::uuid as text), 0));

-- name: OwnerLiveContainers :one
-- The account's two concurrency pools: containers without GPUs, and the
-- cards the others hold. A mirror build's container is the platform's.
select count(*) filter (where c.gpu_count = 0)::int as cpu_containers,
       coalesce(sum(c.gpu_count), 0)::int as gpus
from workspace_members o
join containers c on c.workspace_id = o.workspace_id
left join image_builds b on b.id = c.image_build_id
where o.user_id = @user_id and o.role = 'owner' and c.state <> 'stopped' and b.mirror is not true;

-- name: OwnedWorkspaceCount :one
select count(*)::int from workspace_members where user_id = @user_id and role = 'owner';

-- name: ConnectedCloudCount :one
-- The cloud accounts the account has connected, set up or not.
select count(*)::int from cloud_connections where account_id = @user_id;

-- name: OwnerMemberCount :one
-- Distinct people across the workspaces the account owns, the owner
-- included.
select count(distinct m.user_id)::int
from workspace_members o
join workspace_members m on m.workspace_id = o.workspace_id
where o.user_id = @user_id and o.role = 'owner';

-- name: OwnerHasMember :one
select exists (
    select 1
    from workspace_members o
    join workspace_members m on m.workspace_id = o.workspace_id
    where o.user_id = @owner_id and o.role = 'owner' and m.user_id = @member_id
)::bool;

-- name: OwnerHasMemberEmail :one
select exists (
    select 1
    from workspace_members o
    join workspace_members m on m.workspace_id = o.workspace_id
    join users u on u.id = m.user_id
    where o.user_id = @owner_id and o.role = 'owner' and lower(u.email) = @email::text
)::bool;

-- name: OwnerOpenInvitations :one
-- Open offers to people who are not members yet hold a seat each, so
-- accepting them cannot pass the plan.
select count(distinct i.email)::int
from workspace_members o
join invitations i on i.workspace_id = o.workspace_id
where o.user_id = @owner_id and o.role = 'owner' and i.expires_at > now()
  and not exists (
      select 1
      from workspace_members o2
      join workspace_members m on m.workspace_id = o2.workspace_id
      join users u on u.id = m.user_id
      where o2.user_id = @owner_id and o2.role = 'owner' and lower(u.email) = i.email
  );
