package billing

import (
	"testing"
)

func TestUnfundedAccountsKeepTheirDataThirtyDaysAndAreWarned(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	idle := f.user()
	f.workspace(idle)
	f.account(owner)
	f.account(idle)
	f.exec("insert into volumes (workspace_id, name) values ($1, 'data')", ws)
	f.exec("update billing_balances set balance_nanos = 0")

	sweep := func() RetentionResult {
		t.Helper()
		result, err := f.billing.SweepRetention(t.Context())
		if err != nil {
			t.Fatal(err)
		}
		return result
	}
	// Only the account that stores data starts a period, once.
	if result := sweep(); result.Started != 1 {
		t.Fatalf("started %d periods", result.Started)
	}
	if result := sweep(); result.Started != 0 {
		t.Fatalf("a second sweep started %d periods", result.Started)
	}
	var subject, recipient, state string
	if err := f.pool.QueryRow(t.Context(), `select e.subject, e.recipient, e.state from unfunded_periods p
		join email_outbox e on e.id = p.message_id where p.user_id = $1`, owner).Scan(&subject, &recipient, &state); err != nil {
		t.Fatal(err)
	}
	if subject != "Add credit within 30 days to keep your stored data" || recipient != f.email(owner) || state != "queued" {
		t.Fatalf("warning %q to %q is %s", subject, recipient, state)
	}

	// Credit restored in the window keeps the data and withdraws the
	// unsent warning.
	f.exec("update billing_balances set balance_nanos = 1 where user_id = $1", owner)
	if result := sweep(); result.Ended != 1 {
		t.Fatalf("ended %d periods", result.Ended)
	}
	if err := f.pool.QueryRow(t.Context(), "select state from email_outbox").Scan(&state); err != nil || state != "discarded" {
		t.Fatalf("warning after credit returned: %s %v", state, err)
	}

	// Without credit for the whole period, the workspace's data goes.
	f.exec("update billing_balances set balance_nanos = 0 where user_id = $1", owner)
	sweep()
	if expired, err := f.billing.ExpiredUnfunded(t.Context()); err != nil || len(expired) != 0 {
		t.Fatalf("expired before 30 days: %+v %v", expired, err)
	}
	f.exec("update unfunded_periods set started_at = now() - interval '30 days 1 minute'")
	expired, err := f.billing.ExpiredUnfunded(t.Context())
	if err != nil || len(expired) != 1 || expired[0].User != owner || len(expired[0].Workspaces) != 1 || expired[0].Workspaces[0] != ws {
		t.Fatalf("expired %+v %v", expired, err)
	}
	if err := f.billing.EndRetention(t.Context(), owner); err != nil {
		t.Fatal(err)
	}
	if expired, err := f.billing.ExpiredUnfunded(t.Context()); err != nil || len(expired) != 0 {
		t.Fatalf("after the data is gone: %+v %v", expired, err)
	}
}
