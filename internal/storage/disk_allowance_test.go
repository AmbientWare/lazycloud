package storage_test

import (
	"errors"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/billing"
)

// TestDisksAreHeldToThePlansAllowance: Free allows no disks, and a paid
// plan's allowance covers growing one.
func TestDisksAreHeldToThePlansAllowance(t *testing.T) {
	f := newFixture(t, diskSpec)
	container := f.container()
	if _, err := f.pool.Exec(t.Context(), `
with owner as (select user_id from workspace_members where workspace_id = $1 and role = 'owner'),
     account as (update billing_accounts set complimentary_since = null where user_id in (select user_id from owner))
update billing_balances set balance_nanos = 1000000000 where user_id in (select user_id from owner)`, uuid.UUID(f.ws)); err != nil {
		t.Fatal(err)
	}
	var limit *billing.LimitError
	if _, err := f.storage.AcquireDisk(t.Context(), f.host, container, "root"); !errors.As(err, &limit) {
		t.Fatalf("disk on Free: %v", err)
	}
	if _, err := f.pool.Exec(t.Context(), `update billing_accounts set terms_version = 'team-v3'
		where user_id = (select user_id from workspace_members where workspace_id = $1 and role = 'owner')`, uuid.UUID(f.ws)); err != nil {
		t.Fatal(err)
	}
	if _, err := f.storage.AcquireDisk(t.Context(), f.host, container, "root"); err != nil {
		t.Fatalf("disk on Team: %v", err)
	}
}
