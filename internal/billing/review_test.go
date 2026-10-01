package billing

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// reloadingAccount is an account with automatic reload on, a card and a
// settled balance of balance.
func (f *fixture) reloadingAccount(balance int64) uuid.UUID {
	f.t.Helper()
	owner := f.user()
	f.setAccount(owner, "stripe_customer_id = $2, payment_method_attached_at = now(), reload_enabled = true, reload_threshold_cents = 1000, reload_amount_cents = 2000",
		"cus_"+owner.String())
	f.exec("update credit_lots set expires_at = effective_at + interval '1 second' where user_id = $1", owner)
	f.exec("update billing_balances set balance_nanos = $2, due = false where user_id = $1", owner, balance)
	return owner
}

func TestReloadDecidesFromASettledBalanceOnce(t *testing.T) {
	f := newFixture(t)
	owner := f.reloadingAccount(NanosPerUSD)
	// A reload that already succeeded granted its credit; the balance row
	// has not rolled it in yet.
	f.exec("insert into credit_purchases (user_id, kind, amount_nanos, status) values ($1, 'automatic', $2, 'succeeded')", owner, 20*NanosPerUSD)
	f.grant(creditGrant{user: owner, kind: kindPurchased, source: "purchase:done", amount: 20 * NanosPerUSD, effective: time.Now()})
	if _, started, err := f.billing.startReload(t.Context(), owner); err != nil || started {
		t.Fatalf("reload with $21 settled: started %v, %v", started, err)
	}

	// Concurrent passes over a balance below the threshold start one.
	low := f.reloadingAccount(NanosPerUSD)
	var wg sync.WaitGroup
	var mu sync.Mutex
	started := 0
	for range 6 {
		wg.Go(func() {
			_, ok, err := f.billing.startReload(t.Context(), low)
			if err != nil {
				t.Error(err)
			}
			if ok {
				mu.Lock()
				started++
				mu.Unlock()
			}
		})
	}
	wg.Wait()
	if started != 1 {
		t.Fatalf("%d concurrent reloads started, want 1", started)
	}
}

func TestAutomaticPaymentIsNotRetriedOnceReloadIsOff(t *testing.T) {
	f := newStripeFixture(t)
	owner := f.reloadingAccount(0)
	var id uuid.UUID
	if err := f.pool.QueryRow(t.Context(), "insert into credit_purchases (user_id, kind, amount_nanos) values ($1, 'automatic', $2) returning id",
		owner, 20*NanosPerUSD).Scan(&id); err != nil {
		t.Fatal(err)
	}
	f.setAccount(owner, "reload_enabled = false")
	if err := f.billing.settlePurchase(t.Context(), id); err != nil {
		t.Fatal(err)
	}
	var status string
	if err := f.pool.QueryRow(t.Context(), "select status from credit_purchases where id = $1", id).Scan(&status); err != nil || status != paymentCancelled {
		t.Fatalf("purchase after reload was turned off: %s %v", status, err)
	}
}

func TestMeteringMarksDueBehindAConcurrentRollup(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	f.account(owner)
	stopped := time.Now().UTC()
	f.container(containerSpec{workspace: ws, host: f.host(time.Now()), release: &rel.id, ready: stopped.Add(-time.Minute), stopped: &stopped, cpuMillis: 1000, memoryBytes: 1 << 30})

	// A rollup holds the balance, which is committed as due, and is about
	// to clear it.
	rollup, err := f.pool.Begin(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := rollup.Exec(t.Context(), "update billing_balances set due = false where user_id = $1", owner); err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { _, err := f.billing.Meter(t.Context()); done <- err }()
	time.Sleep(300 * time.Millisecond)
	if err := rollup.Commit(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if !f.balance(owner).due {
		t.Fatal("new cost written behind a rollup left the balance not due")
	}
}

func TestRetentionKeepsDataOfAnAccountFundedOnTheLastDay(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	f.account(owner)
	f.exec("insert into volumes (workspace_id, name) values ($1, 'data')", ws)
	f.exec("update credit_lots set expires_at = effective_at + interval '1 second', effective_at = now() - interval '40 days' where user_id = $1", owner)
	f.exec("update billing_balances set balance_nanos = 0, due = false where user_id = $1", owner)
	f.exec("insert into unfunded_periods (user_id, started_at) values ($1, now() - interval '31 days')", owner)
	// Credit arrives; the balance has not been rolled up yet.
	f.grant(creditGrant{user: owner, kind: kindPurchased, source: "purchase:late", amount: 5 * NanosPerUSD, effective: time.Now()})
	expired, err := f.billing.ExpiredUnfunded(t.Context())
	if err != nil || len(expired) != 0 {
		t.Fatalf("expired %+v, %v; credit added on the last day keeps the data", expired, err)
	}
	f.exec("update billing_balances set balance_nanos = 0, due = false where user_id = $1", owner)
	f.exec("update credit_lots set spent_nanos = amount_nanos where user_id = $1", owner)
	if expired, err := f.billing.ExpiredUnfunded(t.Context()); err != nil || len(expired) != 1 {
		t.Fatalf("still unfunded: %+v %v", expired, err)
	}
}

func TestScheduledDowngradeHoldsAdditionsToTheCheaperPlan(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	f.setAccount(owner, "terms_version = 'team-v3', payment_method_attached_at = now(), scheduled_terms_version = 'free-v2', scheduled_change_at = now() + interval '10 days'")
	ws := f.workspace(owner)
	if err := f.admitWorkspace(owner); !limitReached(err) {
		t.Fatalf("a second workspace while Free is scheduled: %v", err)
	}
	if err := f.admitMember(ws, Candidate{Email: "guest@example.test"}); !limitReached(err) {
		t.Fatalf("a member while Free is scheduled: %v", err)
	}
}

func TestConcurrentWorkspaceCreationsCountEachOther(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	var wg sync.WaitGroup
	for n := range 4 {
		wg.Go(func() {
			_ = pgx.BeginFunc(t.Context(), f.pool, func(tx pgx.Tx) error {
				if err := AdmitWorkspace(t.Context(), tx, owner); err != nil {
					return err
				}
				_, err := tx.Exec(t.Context(), `
with ws as (insert into workspaces (name) values ('race' || $2::int) returning id)
insert into workspace_members (workspace_id, user_id, role) select id, $1, 'owner' from ws`, owner, n)
				return err
			})
		})
	}
	wg.Wait()
	var owned int
	if err := f.pool.QueryRow(t.Context(), "select count(*) from workspace_members where user_id = $1 and role = 'owner'", owner).Scan(&owned); err != nil {
		t.Fatal(err)
	}
	if owned != 1 {
		t.Fatalf("Free account owns %d workspaces after concurrent creations", owned)
	}
}

func TestAContainerReadLiveAndStoppedIsWrittenOnce(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	ws := f.workspace(owner)
	rel := f.release(ws)
	stopped := time.Now().UTC()
	c := f.container(containerSpec{workspace: ws, host: f.host(time.Now()), release: &rel.id, ready: stopped.Add(-2 * time.Minute), stopped: &stopped, cpuMillis: 1000, memoryBytes: 1 << 30})
	// As both reads saw it: still draining to the first, stopped to the
	// second.
	f.exec("update containers set state = 'draining' where id = $1", c)
	if result := f.meter(); result.Failed != 0 || len(f.ledger(c)) == 0 {
		t.Fatalf("meter %+v, %d entries", result, len(f.ledger(c)))
	}
}

// stripeStub stands in for Stripe's API where a test needs an object
// stripe-mock's fixtures cannot hold, and records each request's form.
type stripeStub struct {
	mu       sync.Mutex
	requests map[string][]url.Values
}

func (f *fixture) stubStripe(answer func(request string) any) *stripeStub {
	f.t.Helper()
	stub := &stripeStub{requests: map[string][]url.Values{}}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		request := r.Method + " " + r.URL.Path
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		form, err := url.ParseQuery(string(body))
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		stub.mu.Lock()
		stub.requests[request] = append(stub.requests[request], form)
		stub.mu.Unlock()
		object := answer(request)
		if object == nil {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(object)
	}))
	f.t.Cleanup(server.Close)
	f.billing = NewBilling(f.pool, Config{PublicURL: "https://lazycloud.test",
		Stripe: StripeConfig{SecretKey: "sk_test_stub", WebhookSecret: "whsec_test", APIBase: server.URL}}, slog.New(slog.DiscardHandler))
	return stub
}

func (s *stripeStub) sent(request string) []url.Values {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.requests[request]
}

func TestAPaidReloadWhoseResponseWasLostIsFoundByItsWebhook(t *testing.T) {
	f := newFixture(t)
	owner := f.reloadingAccount(0)
	var id uuid.UUID
	if err := f.pool.QueryRow(t.Context(), "insert into credit_purchases (user_id, kind, amount_nanos) values ($1, 'automatic', $2) returning id",
		owner, 20*NanosPerUSD).Scan(&id); err != nil {
		t.Fatal(err)
	}
	f.stubStripe(func(request string) any {
		if request != "GET /v1/payment_intents/pi_lost" {
			return nil
		}
		return map[string]any{"id": "pi_lost", "object": "payment_intent", "status": "succeeded", "amount": 2000, "amount_received": 2000,
			"customer": "cus_" + owner.String(), "metadata": map[string]string{"credit_purchase_id": id.String()}}
	})
	if err := f.billing.processEvent(t.Context(), "payment_intent.succeeded", "pi_lost", "cus_"+owner.String()); err != nil {
		t.Fatal(err)
	}
	var status string
	var funded int64
	if err := f.pool.QueryRow(t.Context(), `select p.status, coalesce((select sum(l.amount_nanos) from credit_lots l where l.user_id = p.user_id and l.kind = 'purchased'), 0)::bigint
		from credit_purchases p where p.id = $1`, id).Scan(&status, &funded); err != nil {
		t.Fatal(err)
	}
	if status != paymentSucceeded || funded != 20*NanosPerUSD {
		t.Fatalf("purchase %s funded %d", status, funded)
	}
}

func TestThePortalOffersOnlyInvoicesAndCards(t *testing.T) {
	f := newFixture(t)
	stub := f.stubStripe(func(request string) any {
		switch request {
		case "GET /v1/billing_portal/configurations":
			return map[string]any{"object": "list", "data": []any{}, "has_more": false, "url": "/v1/billing_portal/configurations"}
		case "POST /v1/billing_portal/configurations":
			return map[string]any{"id": "bpc_1", "object": "billing_portal.configuration"}
		case "POST /v1/billing_portal/sessions":
			return map[string]any{"id": "bps_1", "object": "billing_portal.session", "url": "https://billing.stripe.com/p/session"}
		}
		return nil
	})
	for range 2 {
		if _, err := f.billing.stripe.portalSession(t.Context(), "cus_1", "https://lazycloud.test/billing"); err != nil {
			t.Fatal(err)
		}
	}
	created := stub.sent("POST /v1/billing_portal/configurations")
	if len(created) != 1 {
		t.Fatalf("created %d portal configurations", len(created))
	}
	for feature, want := range map[string]string{"invoice_history": "true", "payment_method_update": "true", "subscription_update": "false", "subscription_cancel": "false"} {
		if got := created[0].Get("features[" + feature + "][enabled]"); got != want {
			t.Errorf("portal %s enabled %q, want %s", feature, got, want)
		}
	}
	sessions := stub.sent("POST /v1/billing_portal/sessions")
	for _, session := range sessions {
		if session.Get("configuration") != "bpc_1" {
			t.Fatalf("portal session opened with configuration %q", session.Get("configuration"))
		}
	}
	if len(sessions) != 2 {
		t.Fatalf("%d portal sessions", len(sessions))
	}
}
