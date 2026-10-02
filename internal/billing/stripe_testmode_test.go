package billing

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/stripe/stripe-go/v87"
	"github.com/stripe/stripe-go/v87/webhook"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// testModeWebhookSecret signs the deliveries this test hands to
// ReceiveWebhook; Stripe never sends to it.
const testModeWebhookSecret = "whsec_lazycloud_test_mode"

// TestStripeTestMode runs billing's payment flows against real Stripe test
// mode: the card, credit purchase, reload, plan change and portal calls,
// and deliveries of the events Stripe recorded for them. It needs a
// test-mode secret key in LAZYCLOUD_TEST_STRIPE_API_KEY and refuses any
// other key. It deletes the customer it creates; the plan prices and the
// portal configuration stay, as the platform reuses them.
func TestStripeTestMode(t *testing.T) {
	key := os.Getenv("LAZYCLOUD_TEST_STRIPE_API_KEY")
	if key == "" {
		t.Skip("real Stripe test mode: set LAZYCLOUD_TEST_STRIPE_API_KEY to a test-mode secret key")
	}
	if !strings.HasPrefix(key, "sk_test_") && !strings.HasPrefix(key, "rk_test_") {
		t.Fatal("LAZYCLOUD_TEST_STRIPE_API_KEY is not a test-mode key")
	}
	f := newFixture(t)
	f.billing = NewBilling(f.pool, Config{PublicURL: "https://lazycloud.test",
		Stripe: StripeConfig{SecretKey: key, WebhookSecret: testModeWebhookSecret}}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	ctx := t.Context()
	client := f.billing.stripe.client
	started := time.Now().Add(-time.Minute)
	owner := f.user()
	f.account(owner)

	var customer string
	t.Cleanup(func() {
		if customer == "" {
			return
		}
		cleanup, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		if _, err := client.V1Customers.Delete(cleanup, customer, nil); err != nil {
			t.Errorf("delete test customer %s: %v", customer, err)
		}
	})
	stripeIDs := func() (customer, subscription *string) {
		t.Helper()
		ids, err := f.billing.queries.StripeIdentity(ctx, owner)
		if err != nil {
			t.Fatal(err)
		}
		return ids.StripeCustomerID, ids.StripeSubscriptionID
	}

	// deliver signs the event Stripe recorded about object, of kind, and
	// hands it to the webhook endpoint and processing.
	deliver := func(kind stripe.EventType, object string) {
		t.Helper()
		var event *stripe.Event
		for deadline := time.Now().Add(time.Minute); event == nil; {
			params := &stripe.EventListParams{Type: stripe.String(string(kind)), CreatedRange: &stripe.RangeQueryParams{GreaterThanOrEqual: started.Unix()}}
			for e, err := range client.V1Events.List(ctx, params).All(ctx) {
				if err != nil {
					t.Fatalf("list %s events: %v", kind, err)
				}
				if e.GetObjectValue("id") == object {
					event = e
					break
				}
			}
			if event == nil {
				if time.Now().After(deadline) {
					t.Fatalf("stripe recorded no %s event about %s", kind, object)
				}
				time.Sleep(2 * time.Second)
			}
		}
		payload, err := json.Marshal(event)
		if err != nil {
			t.Fatal(err)
		}
		signed := webhook.GenerateTestSignedPayload(&webhook.UnsignedPayload{Payload: payload, Secret: testModeWebhookSecret})
		if err := f.billing.ReceiveWebhook(ctx, signed.Payload, signed.Header); err != nil {
			t.Fatalf("receive %s: %v", kind, err)
		}
		if _, err := f.billing.ProcessEvents(ctx); err != nil {
			t.Fatalf("process %s: %v", kind, err)
		}
		var processed bool
		var lastError string
		if err := f.pool.QueryRow(ctx, "select processed_at is not null, last_error from stripe_events where id = $1", event.ID).Scan(&processed, &lastError); err != nil {
			t.Fatal(err)
		}
		if !processed || lastError != "" {
			t.Fatalf("%s %s: processed %v, error %q", kind, event.ID, processed, lastError)
		}
	}
	hosted := apitypes.HostedSessionRequest{ReturnUrl: "https://lazycloud.test/billing"}

	// The customer is created on first need, by the card setup page.
	if c, _ := stripeIDs(); c != nil {
		t.Fatal("a new account already has a Stripe customer")
	}
	setup, err := f.billing.PaymentMethodSession(ctx, owner, hosted)
	if err != nil || !strings.HasPrefix(setup, "https://checkout.stripe.com/") {
		t.Fatalf("card setup page %q: %v", setup, err)
	}
	c, _ := stripeIDs()
	if c == nil {
		t.Fatal("card setup recorded no customer")
	}
	customer = *c
	if again, err := f.billing.customerFor(ctx, owner); err != nil || again != customer {
		t.Fatalf("second customer %q: %v", again, err)
	}
	created, err := client.V1Customers.Retrieve(ctx, customer, nil)
	if err != nil || created.Metadata["lazycloud_user"] != owner.String() || created.Email != f.email(owner) {
		t.Fatalf("customer %+v: %v", created, err)
	}

	// Stripe's hosted page cannot be completed through the API; a test card
	// attached to the customer raises the same payment_method.attached.
	method, err := client.V1PaymentMethods.Attach(ctx, "pm_card_visa", &stripe.PaymentMethodAttachParams{Customer: stripe.String(customer)})
	if err != nil {
		t.Fatalf("attach test card: %v", err)
	}
	deliver(stripe.EventTypePaymentMethodAttached, method.ID)
	var hasCard bool
	if err := f.pool.QueryRow(ctx, "select payment_method_attached_at is not null from billing_accounts where user_id = $1", owner).Scan(&hasCard); err != nil || !hasCard {
		t.Fatalf("card after delivery: %v %v", hasCard, err)
	}
	if cust, err := client.V1Customers.Retrieve(ctx, customer, nil); err != nil || cust.InvoiceSettings.DefaultPaymentMethod == nil || cust.InvoiceSettings.DefaultPaymentMethod.ID != method.ID {
		t.Fatalf("default card: %v", err)
	}

	// A credit purchase opens Checkout; an abandoned one is cancelled by
	// Stripe's delivery of its expiry.
	purchase, err := f.billing.BuyCredit(ctx, owner, apitypes.CreditPurchaseRequest{AmountCents: 1000, RequestKey: uuid.New(), ReturnUrl: hosted.ReturnUrl})
	if err != nil || purchase.CheckoutUrl == nil || !strings.HasPrefix(*purchase.CheckoutUrl, "https://checkout.stripe.com/") {
		t.Fatalf("credit purchase %+v: %v", purchase, err)
	}
	var session string
	if err := f.pool.QueryRow(ctx, "select checkout_session_id from credit_purchases where id = $1", purchase.Id).Scan(&session); err != nil {
		t.Fatal(err)
	}
	if _, err := client.V1CheckoutSessions.Expire(ctx, session, nil); err != nil {
		t.Fatalf("expire checkout: %v", err)
	}
	deliver(stripe.EventTypeCheckoutSessionExpired, session)
	if p, err := f.billing.CreditPurchase(ctx, owner, purchase.Id); err != nil || p.Status != apitypes.CreditPaymentStatus(paymentCancelled) {
		t.Fatalf("abandoned purchase %+v: %v", p, err)
	}

	// Automatic reload charges the saved card off-session once, and the
	// payment's delivery changes nothing more.
	f.setAccount(owner, "reload_enabled = true, reload_threshold_cents = 1000, reload_amount_cents = 2000")
	if n, err := f.billing.Reload(ctx); err != nil || n != 1 {
		t.Fatalf("reload started %d: %v", n, err)
	}
	var reload uuid.UUID
	var status, intent string
	if err := f.pool.QueryRow(ctx, "select id, status, payment_intent_id from credit_purchases where user_id = $1 and kind = 'automatic'", owner).Scan(&reload, &status, &intent); err != nil {
		t.Fatal(err)
	}
	if status != paymentSucceeded {
		t.Fatalf("automatic reload %s", status)
	}
	deliver(stripe.EventTypePaymentIntentSucceeded, intent)
	purchased := func() (n int, amount int64) {
		t.Helper()
		if err := f.pool.QueryRow(ctx, "select count(*), coalesce(sum(amount_nanos), 0)::bigint from credit_lots where user_id = $1 and kind = 'purchased'", owner).Scan(&n, &amount); err != nil {
			t.Fatal(err)
		}
		return n, amount
	}
	if n, amount := purchased(); n != 1 || amount != 20*NanosPerUSD {
		t.Fatalf("reload funded %d lots of %d", n, amount)
	}
	if n, err := f.billing.Reload(ctx); err != nil || n != 0 {
		t.Fatalf("a funded account reloaded again: %d %v", n, err)
	}

	// Plan changes: subscribe, upgrade with proration, schedule a
	// downgrade, cancel it, schedule Free and cancel that.
	type state struct {
		terms     string
		scheduled *string
		credits   int
	}
	read := func() state {
		t.Helper()
		var s state
		if err := f.pool.QueryRow(ctx, `select a.terms_version, a.scheduled_terms_version,
			(select count(*) from credit_lots l where l.user_id = a.user_id and l.kind = 'subscription')
			from billing_accounts a where a.user_id = $1`, owner).Scan(&s.terms, &s.scheduled, &s.credits); err != nil {
			t.Fatal(err)
		}
		return s
	}
	change := func(plan PlanID, terms TermsVersion) state {
		t.Helper()
		if _, err := f.billing.ChangePlan(ctx, owner, apitypes.PlanChangeRequest{Plan: apitypes.PlanId(plan), TermsVersion: apitypes.TermsVersion(terms)}); err != nil {
			t.Fatalf("change to %s: %v", terms, err)
		}
		return read()
	}
	if s := change(PlanTeam, TermsTeam); s.terms != string(TermsTeam) || s.scheduled != nil || s.credits != 1 {
		t.Fatalf("after subscribing: %+v", s)
	}
	_, sub := stripeIDs()
	if sub == nil {
		t.Fatal("no subscription recorded")
	}
	first, err := client.V1Subscriptions.Retrieve(ctx, *sub, nil)
	if err != nil || first.LatestInvoice == nil {
		t.Fatalf("subscription: %v", err)
	}
	deliver(stripe.EventTypeInvoicePaid, first.LatestInvoice.ID)
	if s := read(); s.credits != 1 {
		t.Fatalf("the invoice's delivery granted its credit again: %+v", s)
	}
	if s := change(PlanBusiness, TermsBusiness); s.terms != string(TermsBusiness) || s.scheduled != nil || s.credits != 2 {
		t.Fatalf("after upgrading: %+v", s)
	}
	if s := change(PlanTeam, TermsTeam); s.terms != string(TermsBusiness) || s.scheduled == nil || *s.scheduled != string(TermsTeam) {
		t.Fatalf("after scheduling Team: %+v", s)
	}
	deliver(stripe.EventTypeCustomerSubscriptionUpdated, *sub)
	if s := read(); s.scheduled == nil || *s.scheduled != string(TermsTeam) {
		t.Fatalf("subscription delivery lost the scheduled change: %+v", s)
	}
	if s := change(PlanBusiness, TermsBusiness); s.terms != string(TermsBusiness) || s.scheduled != nil {
		t.Fatalf("after cancelling the scheduled change: %+v", s)
	}
	if s := change(PlanFree, TermsFree); s.terms != string(TermsBusiness) || s.scheduled == nil || *s.scheduled != string(TermsFree) {
		t.Fatalf("after scheduling Free: %+v", s)
	}
	if s := change(PlanBusiness, TermsBusiness); s.scheduled != nil {
		t.Fatalf("after keeping Business: %+v", s)
	}
	if held, err := client.V1Subscriptions.Retrieve(ctx, *sub, nil); err != nil || held.CancelAtPeriodEnd || held.Schedule != nil {
		t.Fatalf("subscription after keeping Business: cancel at end %v, %v", held.CancelAtPeriodEnd, err)
	}

	// The portal shows invoices and the saved card and nothing else.
	portal, err := f.billing.PortalSession(ctx, owner, hosted)
	if err != nil || !strings.HasPrefix(portal, "https://billing.stripe.com/") {
		t.Fatalf("portal %q: %v", portal, err)
	}
	configuration, err := client.V1BillingPortalConfigurations.Retrieve(ctx, f.billing.stripe.portal, nil)
	if err != nil {
		t.Fatal(err)
	}
	if features := configuration.Features; !features.InvoiceHistory.Enabled || !features.PaymentMethodUpdate.Enabled ||
		features.SubscriptionUpdate.Enabled || features.SubscriptionCancel.Enabled {
		t.Fatalf("portal features %+v", features)
	}
}
