package billing

import (
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

func (f *fixture) admit(req Request) (Grant, error) {
	f.t.Helper()
	var grant Grant
	err := pgx.BeginFunc(f.t.Context(), f.pool, func(tx pgx.Tx) error {
		var err error
		grant, err = Admit(f.t.Context(), tx, req)
		return err
	})
	return grant, err
}

// live inserts n live containers in workspace.
func (f *fixture) live(workspace uuid.UUID, n int) {
	f.t.Helper()
	rel := f.release(workspace)
	f.exec(`insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes)
		select $1, $2, 'pending', 1, 1000, 1 << 30 from generate_series(1, $3)`, workspace, rel.id, n)
}

func (f *fixture) setAccount(user uuid.UUID, assignments string, args ...any) {
	f.t.Helper()
	f.account(user)
	f.exec("update billing_accounts set "+assignments+" where user_id = $1", append([]any{user}, args...)...)
}

func paymentRequired(err error) bool {
	var p *PaymentRequiredError
	return errors.As(err, &p)
}

func limitReached(err error) bool {
	var l *LimitError
	return errors.As(err, &l)
}

func TestAdmitRefusesWorkTheAccountCannotPayFor(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)

	if _, err := f.admit(Request{Workspace: ws, Cold: true}); err != nil {
		t.Fatalf("a new account's trial credit admits work: %v", err)
	}
	f.exec("update billing_balances set balance_nanos = 0 where user_id = $1", owner)
	if _, err := f.admit(Request{Workspace: ws}); !paymentRequired(err) || !strings.Contains(err.Error(), "add credit") {
		t.Fatalf("no credit: %v", err)
	}
	// Accrued cost of live containers counts against the balance.
	f.exec("update billing_balances set balance_nanos = 100, accrued_nanos = 100 where user_id = $1", owner)
	if _, err := f.admit(Request{Workspace: ws}); !paymentRequired(err) {
		t.Fatalf("credit spent by running containers: %v", err)
	}
	f.exec("update billing_balances set balance_nanos = $2, accrued_nanos = 0, month_spent_nanos = $2 where user_id = $1", owner, 5*NanosPerUSD)
	f.setAccount(owner, "monthly_usage_limit_nanos = $2", 5*NanosPerUSD)
	if _, err := f.admit(Request{Workspace: ws}); !paymentRequired(err) || !strings.Contains(err.Error(), "monthly usage limit") {
		t.Fatalf("monthly limit reached: %v", err)
	}
	// A limit of zero blocks new work.
	f.exec("update billing_balances set month_spent_nanos = 0 where user_id = $1", owner)
	f.setAccount(owner, "monthly_usage_limit_nanos = 0")
	if _, err := f.admit(Request{Workspace: ws}); !paymentRequired(err) {
		t.Fatalf("zero limit: %v", err)
	}
	f.setAccount(owner, "monthly_usage_limit_nanos = null, status = 'past_due'")
	if _, err := f.admit(Request{Workspace: ws}); !paymentRequired(err) || !strings.Contains(err.Error(), "did not go through") {
		t.Fatalf("past due: %v", err)
	}
	// A waived account needs no credit and is never past due.
	f.setAccount(owner, "complimentary_since = now()")
	f.exec("update billing_balances set balance_nanos = -1 where user_id = $1", owner)
	if _, err := f.admit(Request{Workspace: ws, Start: 1}); err != nil {
		t.Fatalf("complimentary: %v", err)
	}
}

func TestAdmitCapsContainersAtTheAccountsConcurrency(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	other := f.workspace(owner)
	f.live(ws, 6)
	f.live(other, 3)

	// Without a saved card the account runs at most ten containers across
	// its workspaces.
	grant, err := f.admit(Request{Workspace: ws, Start: 5})
	if err != nil || grant.Start != 1 {
		t.Fatalf("grant %+v, %v; want 1 of 5 under the no-card limit of 10", grant, err)
	}
	f.live(ws, 1)
	if _, err := f.admit(Request{Workspace: other, Cold: true}); !limitReached(err) || !strings.Contains(err.Error(), "most its plan allows (10)") {
		t.Fatalf("cold work at the limit: %v", err)
	}
	if grant, err := f.admit(Request{Workspace: ws, Start: 2}); err != nil || grant.Start != 0 {
		t.Fatalf("grant at the limit %+v, %v", grant, err)
	}
	// A saved card unlocks Free's 30.
	f.setAccount(owner, "payment_method_attached_at = now()")
	if grant, err := f.admit(Request{Workspace: ws, Start: 50}); err != nil || grant.Start != 20 {
		t.Fatalf("grant with a card %+v, %v; want 20", grant, err)
	}
}

func TestConcurrentStartsCountEachOther(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	var wg sync.WaitGroup
	for range 8 {
		wg.Go(func() {
			err := pgx.BeginFunc(t.Context(), f.pool, func(tx pgx.Tx) error {
				grant, err := Admit(t.Context(), tx, Request{Workspace: ws, Start: 3})
				if err != nil {
					return err
				}
				_, err = tx.Exec(t.Context(), `insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes)
					select $1, $2, 'pending', 1, 1000, 1 << 30 from generate_series(1, $3)`, ws, rel.id, grant.Start)
				return err
			})
			if err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	var n int
	if err := f.pool.QueryRow(t.Context(), "select count(*) from containers where workspace_id = $1", ws).Scan(&n); err != nil {
		t.Fatal(err)
	}
	if n != noCardMaxCPUContainers {
		t.Fatalf("%d containers started, want the limit %d", n, noCardMaxCPUContainers)
	}
}

func (f *fixture) admitWorkspace(owner uuid.UUID) error {
	f.t.Helper()
	return pgx.BeginFunc(f.t.Context(), f.pool, func(tx pgx.Tx) error { return AdmitWorkspace(f.t.Context(), tx, owner) })
}

func (f *fixture) admitMember(ws uuid.UUID, who Candidate) error {
	f.t.Helper()
	return pgx.BeginFunc(f.t.Context(), f.pool, func(tx pgx.Tx) error { return AdmitMember(f.t.Context(), tx, ws, who) })
}

func (f *fixture) member(ws, user uuid.UUID) {
	f.t.Helper()
	f.exec("insert into workspace_members (workspace_id, user_id, role) values ($1, $2, 'member')", ws, user)
}

func (f *fixture) email(user uuid.UUID) string {
	f.t.Helper()
	var email string
	if err := f.pool.QueryRow(f.t.Context(), "select email from users where id = $1", user).Scan(&email); err != nil {
		f.t.Fatal(err)
	}
	return email
}

func TestWorkspaceAndMemberLimitsFollowThePlan(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	if err := f.admitWorkspace(owner); err != nil {
		t.Fatalf("an account's first workspace: %v", err)
	}
	ws := f.workspace(owner)
	if err := f.admitWorkspace(owner); !limitReached(err) || !strings.Contains(err.Error(), "owns 1 workspaces") {
		t.Fatalf("Free's second workspace: %v", err)
	}
	if err := f.admitMember(ws, Candidate{Email: "guest@example.test"}); !limitReached(err) {
		t.Fatalf("Free admits only its owner: %v", err)
	}

	// Team: unlimited workspaces and three members, the owner included.
	f.setAccount(owner, "terms_version = 'team-v3', payment_method_attached_at = now()")
	if err := f.admitWorkspace(owner); err != nil {
		t.Fatalf("Team's second workspace: %v", err)
	}
	second := f.workspace(owner)
	alice, bob, carol := f.user(), f.user(), f.user()
	f.member(ws, alice)
	if err := f.admitMember(ws, Candidate{Email: f.email(bob)}); err != nil {
		t.Fatalf("Team's third seat: %v", err)
	}
	// An open invitation holds the last seat.
	f.exec("insert into invitations (workspace_id, email, role, token_hash, expires_at) values ($1, $2, 'member', sha256('i'), now() + interval '1 day')", second, f.email(bob))
	if err := f.admitMember(ws, Candidate{Email: f.email(carol)}); !limitReached(err) || !strings.Contains(err.Error(), "1 open invitations") {
		t.Fatalf("invitation past the seats: %v", err)
	}
	// Accepting counts members only, and someone already in another of the
	// owner's workspaces takes no seat.
	if err := f.admitMember(second, Candidate{User: &bob}); err != nil {
		t.Fatalf("accepting the held seat: %v", err)
	}
	f.member(second, bob)
	if err := f.admitMember(second, Candidate{User: &alice}); err != nil {
		t.Fatalf("a member of another workspace: %v", err)
	}
	if err := f.admitMember(ws, Candidate{User: &carol}); !limitReached(err) || !strings.Contains(err.Error(), "3 members") {
		t.Fatalf("a fourth person: %v", err)
	}

	// A pending plan change holds every plan-limited addition.
	f.exec("insert into plan_changes (user_id, from_terms, to_terms) values ($1, 'team-v3', 'business-v2')", owner)
	var conflict *ConflictError
	if err := f.admitWorkspace(owner); !errors.As(err, &conflict) {
		t.Fatalf("workspace during a plan change: %v", err)
	}
	if err := f.admitMember(ws, Candidate{Email: "x@example.test"}); !errors.As(err, &conflict) {
		t.Fatalf("member during a plan change: %v", err)
	}
}

func TestCapabilitiesFollowThePlan(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	gate := func(fn func(pgx.Tx) error) error {
		t.Helper()
		return pgx.BeginFunc(t.Context(), f.pool, fn)
	}
	domain := func(tx pgx.Tx) error { return AdmitCustomDomain(t.Context(), tx, ws) }
	cloud := func(tx pgx.Tx) error { return AdmitConnectedCloud(t.Context(), tx, owner) }
	disk := func(gib int64) func(pgx.Tx) error {
		return func(tx pgx.Tx) error { return AdmitDisk(t.Context(), tx, ws, gib*bytesPerGiB) }
	}
	if err := gate(domain); !paymentRequired(err) || !strings.Contains(err.Error(), "Team plan") {
		t.Fatalf("domain on Free: %v", err)
	}
	if err := gate(disk(1)); !limitReached(err) || !strings.Contains(err.Error(), "(0 GiB)") {
		t.Fatalf("disk on Free: %v", err)
	}
	f.setAccount(owner, "terms_version = 'team-v3'")
	if err := gate(domain); err != nil {
		t.Fatalf("domain on Team: %v", err)
	}
	if err := gate(disk(1024)); err != nil {
		t.Fatalf("1 TiB of disks on Team: %v", err)
	}
	if err := gate(disk(1025)); !limitReached(err) {
		t.Fatalf("past Team's disks: %v", err)
	}
	if err := gate(cloud); !paymentRequired(err) || !strings.Contains(err.Error(), "Business plan") {
		t.Fatalf("connected cloud on Team: %v", err)
	}
	f.setAccount(owner, "terms_version = 'business-v2'")
	if err := gate(cloud); err != nil {
		t.Fatalf("connected cloud on Business: %v", err)
	}
}

func TestRetentionFollowsThePlan(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	for _, c := range []struct {
		set  string
		days int
	}{
		{"terms_version = 'free-v2'", 1},
		{"terms_version = 'team-v3'", 30},
		{"terms_version = 'business-v2'", 90},
		{"terms_version = 'free-v2', complimentary_since = now()", 90},
	} {
		f.setAccount(owner, c.set)
		got, err := Retention(t.Context(), f.pool, ws)
		if err != nil || got != time.Duration(c.days)*24*time.Hour {
			t.Fatalf("%s: retention %v, %v; want %d days", c.set, got, err, c.days)
		}
	}
	// A workspace whose owner billing has not seen is on Free.
	fresh := f.workspace(f.user())
	if got, err := Retention(t.Context(), f.pool, fresh); err != nil || got != 24*time.Hour {
		t.Fatalf("no account: %v, %v", got, err)
	}
}

func TestPlanChangesCannotGoBelowHeldLimits(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	f.setAccount(owner, "terms_version = 'team-v3', payment_method_attached_at = now()")
	ws := f.workspace(owner)
	f.workspace(owner)
	f.member(ws, f.user())
	free, err := PlanFor(PlanFree)
	if err != nil {
		t.Fatal(err)
	}
	err = planFits(t.Context(), f.billing.queries, owner, free, true)
	var conflict *ConflictError
	if !errors.As(err, &conflict) || !strings.Contains(err.Error(), "2 workspaces (limit 1)") || !strings.Contains(err.Error(), "2 members (limit 1)") {
		t.Fatalf("moving to Free with two workspaces and a member: %v", err)
	}
	business, err := PlanFor(PlanBusiness)
	if err != nil {
		t.Fatal(err)
	}
	if err := planFits(t.Context(), f.billing.queries, owner, business, true); err != nil {
		t.Fatalf("moving up: %v", err)
	}
}
