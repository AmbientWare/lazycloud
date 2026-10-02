package billing

import (
	"testing"
	"time"

	"github.com/google/uuid"
)

// usage meters one stopped container of owner that cost about nanos and
// returns its exact cost.
func (f *fixture) usage(owner uuid.UUID, ready time.Time, d time.Duration, cpuMillis int64) int64 {
	f.t.Helper()
	ws := f.workspace(owner)
	rel := f.release(ws)
	stopped := ready.Add(d)
	c := f.container(containerSpec{workspace: ws, host: f.host(time.Now()), release: &rel.id, ready: ready, stopped: &stopped, cpuMillis: cpuMillis, memoryBytes: 1 << 30})
	f.meter()
	var cost int64
	for _, e := range f.ledger(c) {
		cost += e.cost
	}
	return cost
}

func (f *fixture) account(user uuid.UUID) {
	f.t.Helper()
	if err := ensureAccount(f.t.Context(), f.billing.queries, user); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fixture) grant(g creditGrant) uuid.UUID {
	f.t.Helper()
	id, err := addCredit(f.t.Context(), f.billing.queries, g)
	if err != nil {
		f.t.Fatal(err)
	}
	return id
}

func TestTrialCoversUsageAndNewCreditPaysDebtFirst(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	start := time.Now().UTC().Truncate(time.Hour).Add(-5 * time.Hour)
	// 64 cores for two hours costs more than the $2 trial.
	cost := f.usage(owner, start, 2*time.Hour, 64_000)
	if cost <= TrialNanos {
		t.Fatalf("usage %d does not exceed the trial", cost)
	}
	f.rollup()
	if b := f.balance(owner); b.balance != TrialNanos-cost || b.due {
		t.Fatalf("balance %+v, want %d", b, TrialNanos-cost)
	}

	// Purchased later, it covers the debt before anything else.
	f.grant(creditGrant{user: owner, kind: kindPurchased, source: "purchase:1", amount: 5 * NanosPerUSD, effective: time.Now()})
	if !f.balance(owner).due {
		t.Fatal("a grant does not make the balance due")
	}
	f.rollup()
	if b := f.balance(owner); b.balance != 7*NanosPerUSD-cost {
		t.Fatalf("balance after purchase %d, want %d", b.balance, 7*NanosPerUSD-cost)
	}
	var uncovered int64
	if err := f.pool.QueryRow(t.Context(), "select coalesce(sum(cost_nanos - credited_nanos - waived_nanos), 0) from billing_hours where user_id = $1", owner).Scan(&uncovered); err != nil {
		t.Fatal(err)
	}
	if uncovered != 0 {
		t.Fatalf("%d nanodollars of usage left uncovered", uncovered)
	}
	// Granting the same source again changes nothing.
	f.grant(creditGrant{user: owner, kind: kindPurchased, source: "purchase:1", amount: 5 * NanosPerUSD, effective: time.Now()})
	f.rollup()
	if b := f.balance(owner); b.balance != 7*NanosPerUSD-cost {
		t.Fatalf("a repeated grant moved the balance to %d", b.balance)
	}
}

func TestExpiredCreditCoversOnlyUsageBeforeItsExpiry(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	early := time.Now().UTC().Truncate(time.Hour).Add(-6 * time.Hour)
	before := f.usage(owner, early, 10*time.Minute, 1000)
	// The trial expired between the two usages; the rollup runs after both.
	f.exec("update credit_lots set effective_at = $2, expires_at = $3 where user_id = $1 and kind = 'trial'",
		owner, early.Add(-24*time.Hour), early.Add(2*time.Hour))
	after := f.usage(owner, early.Add(3*time.Hour), 10*time.Minute, 1000)
	f.rollup()
	if b := f.balance(owner); b.balance != -after {
		t.Fatalf("balance %d, want %d: the expired trial covers %d of earlier usage and none after", b.balance, -after, before)
	}
}

func TestSubscriptionCreditIsSpentBeforePurchasedCredit(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	f.account(owner)
	f.exec("update credit_lots set effective_at = now() - interval '10 days', expires_at = now() - interval '9 days' where user_id = $1", owner)
	now := time.Now().UTC()
	periodEnd := now.Add(20 * 24 * time.Hour)
	f.grant(creditGrant{user: owner, kind: kindPurchased, source: "purchase:1", amount: 10 * NanosPerUSD, effective: now.Add(-48 * time.Hour)})
	f.grant(creditGrant{user: owner, kind: kindSubscription, source: "invoice:1", amount: 25 * NanosPerUSD, effective: now.Add(-48 * time.Hour), expires: &periodEnd})
	cost := f.usage(owner, now.Truncate(time.Hour).Add(-3*time.Hour), time.Hour, 8000)
	f.rollup()
	var subscription, purchased int64
	if err := f.pool.QueryRow(t.Context(), `
select coalesce(sum(spent_nanos) filter (where kind = 'subscription'), 0), coalesce(sum(spent_nanos) filter (where kind = 'purchased'), 0)
from credit_lots where user_id = $1`, owner).Scan(&subscription, &purchased); err != nil {
		t.Fatal(err)
	}
	if subscription != cost || purchased != 0 {
		t.Fatalf("spent %d subscription and %d purchased credit on %d of usage", subscription, purchased, cost)
	}
	var covered int64
	if err := f.pool.QueryRow(t.Context(), "select sum(subscription_nanos) from billing_hours where user_id = $1", owner).Scan(&covered); err != nil {
		t.Fatal(err)
	}
	if covered != cost {
		t.Fatalf("subscription coverage %d, want %d", covered, cost)
	}
}

func TestComplimentaryUsageIsPricedAndWaived(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	start := time.Now().UTC().Truncate(time.Hour).Add(-4 * time.Hour)
	f.exec("insert into billing_accounts (user_id, complimentary_since) values ($1, $2)", owner, start.Add(-time.Hour))
	f.exec("insert into billing_balances (user_id, balance_nanos, month_started_at, recheck_at) values ($1, 0, now(), now())", owner)
	cost := f.usage(owner, start, 2*time.Hour, 64_000)
	f.rollup()
	var waived int64
	if err := f.pool.QueryRow(t.Context(), "select sum(waived_nanos) from billing_hours where user_id = $1", owner).Scan(&waived); err != nil {
		t.Fatal(err)
	}
	if b := f.balance(owner); waived != cost || b.balance != 0 || b.monthSpent == 0 && start.Month() == time.Now().UTC().Month() {
		t.Fatalf("waived %d of %d; balance %+v", waived, cost, b)
	}
}

func TestReversalPastSpentCreditIsPaidFromOtherCredit(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	f.account(owner)
	f.exec("update credit_lots set effective_at = now() - interval '10 days', expires_at = now() - interval '9 days' where user_id = $1", owner)
	now := time.Now().UTC()
	refunded := f.grant(creditGrant{user: owner, kind: kindPurchased, source: "purchase:1", amount: 5 * NanosPerUSD, effective: now.Add(-48 * time.Hour)})
	cost := f.usage(owner, now.Truncate(time.Hour).Add(-3*time.Hour), time.Hour, 32_000)
	f.rollup()
	if err := f.billing.queries.ReverseCredit(t.Context(), ReverseCreditParams{ID: refunded, ReversedNanos: 5 * NanosPerUSD}); err != nil {
		t.Fatal(err)
	}
	f.rollup()
	if b := f.balance(owner); b.balance != -cost {
		t.Fatalf("balance after refund %d, want %d", b.balance, -cost)
	}
	f.grant(creditGrant{user: owner, kind: kindPurchased, source: "purchase:2", amount: 20 * NanosPerUSD, effective: now})
	f.rollup()
	if b := f.balance(owner); b.balance != 20*NanosPerUSD-cost {
		t.Fatalf("balance after a new purchase %d, want %d", b.balance, 20*NanosPerUSD-cost)
	}
}
