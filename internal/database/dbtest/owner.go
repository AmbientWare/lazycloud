package dbtest

import (
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

// Waive gives users billing accounts with their usage charges waived, so
// plan limits and credit do not refuse the work of fixtures that insert
// users directly; billing tests those.
func Waive(t testing.TB, pool *pgxpool.Pool, users ...uuid.UUID) {
	t.Helper()
	_, err := pool.Exec(t.Context(), `
with account as (
    insert into billing_accounts (user_id, complimentary_since) select unnest($1::uuid[]), now()
)
insert into billing_balances (user_id, month_started_at, recheck_at)
select unnest($1::uuid[]), date_trunc('month', now(), 'UTC'), now() + interval '1 day'`, users)
	if err != nil {
		t.Fatalf("waive users: %v", err)
	}
}

// OwnWorkspaces gives every workspace without one the owner each workspace
// has, with its usage charges waived. Billing admits a workspace's work
// against its owner's account, and a waived account needs no credit and is
// held to Business limits, so fixtures that insert workspaces directly are
// not held to plan limits; billing tests those.
func OwnWorkspaces(t testing.TB, pool *pgxpool.Pool) {
	t.Helper()
	_, err := pool.Exec(t.Context(), `
with ownerless as (
    select w.id as workspace_id, gen_random_uuid() as user_id
    from workspaces w
    where not exists (select 1 from workspace_members m where m.workspace_id = w.id and m.role = 'owner')
), owner as (
    insert into users (id, email) select user_id, 'owner-' || user_id || '@example.test' from ownerless returning id
), member as (
    insert into workspace_members (workspace_id, user_id, role) select workspace_id, user_id, 'owner' from ownerless
), account as (
    insert into billing_accounts (user_id, complimentary_since) select id, now() from owner
)
insert into billing_balances (user_id, month_started_at, recheck_at)
select id, date_trunc('month', now(), 'UTC'), now() + interval '1 day' from owner`)
	if err != nil {
		t.Fatalf("give workspaces their owners: %v", err)
	}
}
