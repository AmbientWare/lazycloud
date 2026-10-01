package billing

import (
	"errors"
	"io"
	"log/slog"
	"net"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stripe/stripe-go/v87"
	"github.com/stripe/stripe-go/v87/webhook"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// stripeMock is stripe-mock, which validates every request against
// Stripe's API description and answers with fixture objects. Start it with
// `docker run -d -p 127.0.0.1:12111:12111 stripe/stripe-mock`, or point
// LAZYCLOUD_TEST_STRIPE_URL at another one.
func stripeMock(t *testing.T) StripeConfig {
	t.Helper()
	base := os.Getenv("LAZYCLOUD_TEST_STRIPE_URL")
	if base == "" {
		base = "http://127.0.0.1:12111"
	}
	dialer := net.Dialer{Timeout: time.Second}
	conn, err := dialer.DialContext(t.Context(), "tcp", strings.TrimPrefix(base, "http://"))
	if err != nil {
		t.Fatalf("stripe-mock at %s (docker run -d -p 127.0.0.1:12111:12111 stripe/stripe-mock): %v", base, err)
	}
	_ = conn.Close()
	return StripeConfig{SecretKey: "sk_test_mock", WebhookSecret: "whsec_test", APIBase: base}
}

func newStripeFixture(t *testing.T) *fixture {
	t.Helper()
	f := newFixture(t)
	f.billing = NewBilling(f.pool, Config{PublicURL: "https://lazycloud.test", Stripe: stripeMock(t)}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	return f
}

// unmatched reports whether err is only this platform declining a fixture
// object Stripe returned, which means Stripe accepted the request.
func unmatched(err error) bool {
	return err != nil && strings.Contains(err.Error(), "holds no published plan price")
}

// TestStripeAcceptsEveryRequest sends each call billing makes to
// stripe-mock, which refuses parameters Stripe's API does not take.
func TestStripeAcceptsEveryRequest(t *testing.T) {
	f := newStripeFixture(t)
	s := f.billing.stripe
	ctx := t.Context()
	user := f.user()

	customer, err := s.customer(ctx, user, "payer@example.test")
	if err != nil || customer == "" {
		t.Fatalf("customer %q: %v", customer, err)
	}
	if url, err := s.setupSession(ctx, customer, "https://lazycloud.test/a", "https://lazycloud.test/b"); err != nil || url == "" {
		t.Fatalf("card setup session %q: %v", url, err)
	}
	if url, err := s.portalSession(ctx, customer, "https://lazycloud.test/a"); err != nil || url == "" {
		t.Fatalf("portal session %q: %v", url, err)
	}
	if session, err := s.creditCheckout(ctx, customer, uuid.New(), 2500, "https://lazycloud.test/a", "https://lazycloud.test/b"); err != nil || session.id == "" {
		t.Fatalf("credit checkout %+v: %v", session, err)
	}
	if _, err := s.checkoutPayment(ctx, "cs_test"); err != nil {
		t.Fatalf("read checkout: %v", err)
	}
	if _, err := s.chargeCard(ctx, customer, uuid.New(), 2000); err != nil {
		t.Fatalf("charge card: %v", err)
	}
	if _, err := s.syncCard(ctx, customer); err != nil {
		t.Fatalf("sync card: %v", err)
	}
	for _, v := range []TermsVersion{TermsTeam, TermsBusiness} {
		if price, err := s.price(ctx, v); err != nil || price == "" {
			t.Fatalf("price of %s %q: %v", v, price, err)
		}
	}
	if _, err := s.subscribe(ctx, customer, TermsTeam, "plan-change-test"); !unmatched(err) {
		t.Fatalf("subscribe: %v", err)
	}
	held := subscription{id: "sub_test", item: "si_test", terms: TermsTeam, schedule: "sub_sched_test",
		periodStart: time.Now().Add(-time.Hour), periodEnd: time.Now().Add(29 * 24 * time.Hour)}
	if _, err := s.upgrade(ctx, held, TermsBusiness, "plan-change-up"); !unmatched(err) {
		t.Fatalf("upgrade: %v", err)
	}
	if _, err := s.scheduleDowngrade(ctx, held, TermsTeam, "plan-change-down"); !unmatched(err) {
		t.Fatalf("schedule a cheaper plan: %v", err)
	}
	if _, err := s.scheduleDowngrade(ctx, held, TermsFree, "plan-change-free"); !unmatched(err) {
		t.Fatalf("schedule Free: %v", err)
	}
	if _, err := s.keep(ctx, subscription{id: "sub_test", cancelAtEnd: true}, "plan-change-keep"); !unmatched(err) {
		t.Fatalf("keep: %v", err)
	}
	if inv, err := s.invoice(ctx, "in_test"); err != nil || inv.id == "" {
		t.Fatalf("invoice %+v: %v", inv, err)
	}
}

func TestPaymentOperationsNeedStripe(t *testing.T) {
	f := newFixture(t)
	user := f.user()
	if _, err := f.billing.PaymentMethodSession(t.Context(), user, hosted("https://lazycloud.test/billing")); !errors.Is(err, ErrPaymentsUnavailable) {
		t.Fatalf("card session without Stripe: %v", err)
	}
	if err := f.billing.ReceiveWebhook(t.Context(), []byte("{}"), ""); !errors.Is(err, ErrPaymentsUnavailable) {
		t.Fatalf("webhook without Stripe: %v", err)
	}
}

func TestReturnAddressesStayOnThePlatform(t *testing.T) {
	f := newStripeFixture(t)
	user := f.user()
	for _, target := range []string{"https://evil.test/billing", "http://lazycloud.test/billing", "://"} {
		var invalid *InvalidError
		if _, err := f.billing.PaymentMethodSession(t.Context(), user, hosted(target)); !errors.As(err, &invalid) {
			t.Fatalf("return address %q: %v", target, err)
		}
	}
	url, err := f.billing.PaymentMethodSession(t.Context(), user, hosted("https://lazycloud.test/billing?settings=billing"))
	if err != nil || url == "" {
		t.Fatalf("card session: %q %v", url, err)
	}
	// The customer is recorded once and reused.
	var customer string
	if err := f.pool.QueryRow(t.Context(), "select stripe_customer_id from billing_accounts where user_id = $1", user).Scan(&customer); err != nil || customer == "" {
		t.Fatalf("customer %q: %v", customer, err)
	}
	if again, err := f.billing.customerFor(t.Context(), user); err != nil || again != customer {
		t.Fatalf("second lookup %q, %v; want %q", again, err, customer)
	}
}

func TestWebhooksAreVerifiedAndStoredOnce(t *testing.T) {
	f := newStripeFixture(t)
	payload := []byte(`{"id": "evt_1", "object": "event", "api_version": "2020-08-27", "type": "payment_method.detached",
		"data": {"object": {"id": "pm_1", "object": "payment_method", "customer": null},
		         "previous_attributes": {"customer": "cus_1"}}}`)
	signed := webhook.GenerateTestSignedPayload(&webhook.UnsignedPayload{Payload: payload, Secret: "whsec_test"})
	if err := f.billing.ReceiveWebhook(t.Context(), payload, "t=1,v1=00"); !errors.Is(err, ErrBadSignature) {
		t.Fatalf("forged delivery: %v", err)
	}
	other := webhook.GenerateTestSignedPayload(&webhook.UnsignedPayload{Payload: payload, Secret: "whsec_other"})
	if err := f.billing.ReceiveWebhook(t.Context(), payload, other.Header); !errors.Is(err, ErrBadSignature) {
		t.Fatalf("delivery signed with another secret: %v", err)
	}
	for range 2 {
		if err := f.billing.ReceiveWebhook(t.Context(), payload, signed.Header); err != nil {
			t.Fatalf("delivery: %v", err)
		}
	}
	var n int
	var object, customer string
	if err := f.pool.QueryRow(t.Context(), "select count(*), min(object_id), min(customer_id) from stripe_events").Scan(&n, &object, &customer); err != nil {
		t.Fatal(err)
	}
	// A detached method no longer names its customer; the delivery's
	// previous attributes do.
	if n != 1 || object != "pm_1" || customer != "cus_1" {
		t.Fatalf("stored %d events, object %q, customer %q", n, object, customer)
	}
	// Processing fetches the customer named; it belongs to no account
	// here, so the event settles without changing anything.
	if processed, err := f.billing.ProcessEvents(t.Context()); err != nil || processed != 1 {
		t.Fatalf("processed %d: %v", processed, err)
	}
	var done bool
	if err := f.pool.QueryRow(t.Context(), "select processed_at is not null from stripe_events").Scan(&done); err != nil || !done {
		t.Fatalf("event processed %v: %v", done, err)
	}
}

func TestPaymentOutcomesFundOnceAndTakeBackReversals(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	f.account(owner)
	f.setAccount(owner, "stripe_customer_id = 'cus_1', payment_method_attached_at = now(), reload_enabled = true")
	var id uuid.UUID
	if err := f.pool.QueryRow(t.Context(), "insert into credit_purchases (user_id, kind, amount_nanos) values ($1, 'automatic', $2) returning id",
		owner, 20*NanosPerUSD).Scan(&id); err != nil {
		t.Fatal(err)
	}
	record := func(p payment) {
		t.Helper()
		row, err := f.billing.queries.Purchase(t.Context(), id)
		if err != nil {
			t.Fatal(err)
		}
		if err := recordPayment(t.Context(), f.billing.queries, row, p); err != nil {
			t.Fatal(err)
		}
	}
	lots := func() (n int, amount, reversed int64) {
		t.Helper()
		if err := f.pool.QueryRow(t.Context(), "select count(*), coalesce(sum(amount_nanos), 0), coalesce(sum(reversed_nanos), 0) from credit_lots where user_id = $1 and kind = 'purchased'",
			owner).Scan(&n, &amount, &reversed); err != nil {
			t.Fatal(err)
		}
		return n, amount, reversed
	}
	succeeded := payment{intent: "pi_1", purchase: id.String(), status: paymentSucceeded, amount: 2000, retained: 2000}
	record(succeeded)
	record(succeeded)
	if n, amount, _ := lots(); n != 1 || amount != 20*NanosPerUSD {
		t.Fatalf("funded %d lots of %d", n, amount)
	}
	// A partial refund, then a lost dispute of the rest.
	succeeded.retained = 1500
	record(succeeded)
	if _, _, reversed := lots(); reversed != 5*NanosPerUSD {
		t.Fatalf("reversed %d after a $5 refund", reversed)
	}
	succeeded.retained = 0
	record(succeeded)
	if _, _, reversed := lots(); reversed != 20*NanosPerUSD {
		t.Fatalf("reversed %d after the dispute", reversed)
	}

	// A declined automatic payment pauses reload until the account resumes.
	var declined uuid.UUID
	if err := f.pool.QueryRow(t.Context(), "insert into credit_purchases (user_id, kind, amount_nanos) values ($1, 'automatic', $2) returning id",
		owner, 20*NanosPerUSD).Scan(&declined); err != nil {
		t.Fatal(err)
	}
	row, err := f.billing.queries.Purchase(t.Context(), declined)
	if err != nil {
		t.Fatal(err)
	}
	if err := recordPayment(t.Context(), f.billing.queries, row, payment{purchase: declined.String(), status: paymentDeclined}); err != nil {
		t.Fatal(err)
	}
	account, err := f.billing.Account(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if r := account.AutomaticReload; r.PausedPurchaseId == nil || *r.PausedPurchaseId != declined || r.PauseReason == nil || *r.PauseReason != "declined" {
		t.Fatalf("reload after a decline: %+v", r)
	}
	if account, err = f.billing.ResumeReload(t.Context(), owner); err != nil || account.AutomaticReload.PauseReason != nil {
		t.Fatalf("resume: %+v %v", account.AutomaticReload, err)
	}
}

func TestSubscriptionsSetTheAccountsPlan(t *testing.T) {
	f := newFixture(t)
	owner := f.user()
	f.account(owner)
	apply := func(sub subscription) {
		t.Helper()
		if err := applySubscription(t.Context(), f.billing.queries, owner, sub); err != nil {
			t.Fatal(err)
		}
	}
	view := func() (string, string, *string) {
		t.Helper()
		var terms, status string
		var sub *string
		if err := f.pool.QueryRow(t.Context(), "select terms_version, status, stripe_subscription_id from billing_accounts where user_id = $1", owner).Scan(&terms, &status, &sub); err != nil {
			t.Fatal(err)
		}
		return terms, status, sub
	}
	start := time.Now().UTC().Truncate(time.Second)
	free := TermsFree
	at := start.Add(30 * 24 * time.Hour)
	team := subscription{id: "sub_1", status: stripe.SubscriptionStatusActive, terms: TermsTeam, periodStart: start, periodEnd: at}
	apply(team)
	if terms, status, sub := view(); terms != "team-v3" || status != "active" || sub == nil || *sub != "sub_1" {
		t.Fatalf("subscribed: %s %s %v", terms, status, sub)
	}
	team.status = stripe.SubscriptionStatusPastDue
	apply(team)
	if _, status, _ := view(); status != "past_due" {
		t.Fatalf("payment failed: %s", status)
	}
	team.status, team.scheduled, team.scheduledAt = stripe.SubscriptionStatusActive, &free, &at
	apply(team)
	account, err := f.billing.Account(t.Context(), owner)
	if err != nil || account.Plan.ScheduledTermsVersion == nil || *account.Plan.ScheduledTermsVersion != "free-v2" || account.Status != "active" {
		t.Fatalf("scheduled downgrade: %+v %v", account.Plan, err)
	}
	// A delivery about another subscription changes nothing.
	apply(subscription{id: "sub_2", status: stripe.SubscriptionStatusCanceled, terms: TermsBusiness})
	if terms, _, sub := view(); terms != "team-v3" || *sub != "sub_1" {
		t.Fatalf("another subscription moved the account to %s %v", terms, sub)
	}
	team.status = stripe.SubscriptionStatusCanceled
	apply(team)
	if terms, status, sub := view(); terms != "free-v2" || status != "active" || sub != nil {
		t.Fatalf("ended: %s %s %v", terms, status, sub)
	}
}

func TestIncludedCreditForNewUpgradedAndRenewedPeriods(t *testing.T) {
	start := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	end := start.AddDate(0, 1, 0)
	half := start.Add(end.Sub(start) / 2)
	full := invoice{id: "in_1", paid: true, paidAt: start, lines: []invoiceLine{{terms: TermsTeam, amount: 4900, periodStart: start, periodEnd: end}}}
	if amount, effective, ok := includedCredit(full, start, end); !ok || amount != 25*NanosPerUSD || !effective.Equal(start) {
		t.Fatalf("new Team period: %d from %s", amount, effective)
	}
	// Halfway through, Team to Business: Business's credit for the time
	// left less Team's.
	upgrade := invoice{id: "in_2", paid: true, paidAt: half, lines: []invoiceLine{
		{terms: TermsTeam, amount: -2450, proration: true, periodStart: half, periodEnd: end},
		{terms: TermsBusiness, amount: 9950, proration: true, periodStart: half, periodEnd: end},
	}}
	amount, effective, ok := includedCredit(upgrade, start, end)
	if want := (100 - 25) * NanosPerUSD / 2; !ok || amount != want || !effective.Equal(half) {
		t.Fatalf("upgrade: %d from %s, want %d from %s", amount, effective, want, half)
	}
	// Lines of another period grant nothing for this one.
	stale := invoice{id: "in_3", paid: true, lines: []invoiceLine{{terms: TermsTeam, amount: 4900, periodStart: start.AddDate(0, -1, 0), periodEnd: start}}}
	if _, _, ok := includedCredit(stale, start, end); ok {
		t.Fatal("a previous period's line granted credit")
	}
}

func hosted(url string) apitypes.HostedSessionRequest {
	return apitypes.HostedSessionRequest{ReturnUrl: url}
}
