package billing

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/google/uuid"
	"github.com/stripe/stripe-go/v87"
)

// StripeConfig configures the Stripe provider.
type StripeConfig struct {
	SecretKey     string
	WebhookSecret string
	// APIBase overrides https://api.stripe.com, for stripe-mock in tests.
	APIBase string
}

// Stripe price lookup keys of the paid plans. The prices are created on
// first use when the Stripe account has none.
func priceLookupKey(v TermsVersion) string { return "lazycloud-" + string(v) }

// stripeTimeout bounds one Stripe call; the client retries network
// failures twice under the same idempotency key.
const stripeTimeout = 30 * time.Second

// stripeProvider makes the Stripe calls billing needs. Every call that
// creates or changes something carries an idempotency key derived from a
// row the caller committed first, and no call runs inside a database
// transaction.
type stripeProvider struct {
	client        *stripe.Client
	webhookSecret string

	// prices maps paid terms to their Stripe price id, resolved once per
	// process; a price never changes once created.
	mu     sync.Mutex
	prices map[TermsVersion]string
	// portal is the billing portal configuration sessions open with.
	portal string
}

// portalConfigurationName marks the portal configuration this platform
// creates: invoices and payment methods only. Plan changes and
// cancellations go through ChangePlan, which checks the account fits.
const portalConfigurationName = "lazycloud-invoices-and-cards-v1"

func newStripeProvider(cfg StripeConfig) *stripeProvider {
	backend := &stripe.BackendConfig{
		HTTPClient:        &http.Client{Timeout: stripeTimeout},
		MaxNetworkRetries: stripe.Int64(2),
		LeveledLogger:     &stripe.LeveledLogger{Level: stripe.LevelNull},
	}
	if cfg.APIBase != "" {
		backend.URL = stripe.String(cfg.APIBase)
	}
	return &stripeProvider{
		client:        stripe.NewClient(cfg.SecretKey, stripe.WithBackends(stripe.NewBackendsWithConfig(backend))),
		webhookSecret: cfg.WebhookSecret,
		prices:        map[TermsVersion]string{},
	}
}

// refusal reports whether err is Stripe declining the request itself — a
// card error or an invalid request — rather than an outcome nobody knows.
func refusal(err error) (*stripe.Error, bool) {
	var se *stripe.Error
	if errors.As(err, &se) && (se.Type == stripe.ErrorTypeCard || se.Type == stripe.ErrorTypeInvalidRequest) {
		return se, true
	}
	return nil, false
}

func keyed(params interface{ SetIdempotencyKey(string) }, key string) {
	params.SetIdempotencyKey(key)
}

// customer returns the account's Stripe customer, creating it: a search by
// the account's metadata finds one an earlier attempt made after its
// idempotency key expired.
func (s *stripeProvider) customer(ctx context.Context, user uuid.UUID, email string) (string, error) {
	search := &stripe.CustomerSearchParams{SearchParams: stripe.SearchParams{
		Query: fmt.Sprintf("metadata['lazycloud_user']:'%s'", user),
	}}
	for found, err := range s.client.V1Customers.Search(ctx, search).All(ctx) {
		if err != nil {
			return "", fmt.Errorf("search stripe customers: %w", err)
		}
		if found.Metadata["lazycloud_user"] == user.String() {
			return found.ID, nil
		}
	}
	params := &stripe.CustomerCreateParams{Metadata: map[string]string{"lazycloud_user": user.String()}}
	if email != "" {
		params.Email = stripe.String(email)
	}
	keyed(params, "customer-"+user.String())
	created, err := s.client.V1Customers.Create(ctx, params)
	if err != nil {
		return "", fmt.Errorf("create stripe customer: %w", err)
	}
	return created.ID, nil
}

func (s *stripeProvider) setupSession(ctx context.Context, customer, success, cancel string) (string, error) {
	params := &stripe.CheckoutSessionCreateParams{
		Mode: stripe.String(string(stripe.CheckoutSessionModeSetup)), Customer: stripe.String(customer),
		Currency: stripe.String(strings.ToLower(Currency)), SuccessURL: stripe.String(success), CancelURL: stripe.String(cancel),
	}
	// Cards only: other wallets cannot be charged off-session, which is
	// what a saved method is for.
	params.AddExtra("payment_method_types[0]", "card")
	session, err := s.client.V1CheckoutSessions.Create(ctx, params)
	if err != nil {
		return "", fmt.Errorf("create card setup session: %w", err)
	}
	return session.URL, nil
}

func (s *stripeProvider) portalSession(ctx context.Context, customer, returnURL string) (string, error) {
	configuration, err := s.portalConfiguration(ctx)
	if err != nil {
		return "", err
	}
	session, err := s.client.V1BillingPortalSessions.Create(ctx, &stripe.BillingPortalSessionCreateParams{
		Customer: stripe.String(customer), ReturnURL: stripe.String(returnURL), Configuration: stripe.String(configuration),
	})
	if err != nil {
		return "", fmt.Errorf("create billing portal session: %w", err)
	}
	return session.URL, nil
}

// portalConfiguration returns the portal configuration that shows invoices
// and saved cards and nothing else, creating it once per Stripe account.
func (s *stripeProvider) portalConfiguration(ctx context.Context) (string, error) {
	s.mu.Lock()
	id := s.portal
	s.mu.Unlock()
	if id != "" {
		return id, nil
	}
	for c, err := range s.client.V1BillingPortalConfigurations.List(ctx, &stripe.BillingPortalConfigurationListParams{Active: stripe.Bool(true)}).All(ctx) {
		if err != nil {
			return "", fmt.Errorf("list portal configurations: %w", err)
		}
		if c.Metadata["lazycloud"] == portalConfigurationName {
			id = c.ID
			break
		}
	}
	if id == "" {
		params := &stripe.BillingPortalConfigurationCreateParams{
			Metadata: map[string]string{"lazycloud": portalConfigurationName},
			Features: &stripe.BillingPortalConfigurationCreateFeaturesParams{
				InvoiceHistory:      &stripe.BillingPortalConfigurationCreateFeaturesInvoiceHistoryParams{Enabled: stripe.Bool(true)},
				PaymentMethodUpdate: &stripe.BillingPortalConfigurationCreateFeaturesPaymentMethodUpdateParams{Enabled: stripe.Bool(true)},
				SubscriptionCancel:  &stripe.BillingPortalConfigurationCreateFeaturesSubscriptionCancelParams{Enabled: stripe.Bool(false)},
				SubscriptionUpdate:  &stripe.BillingPortalConfigurationCreateFeaturesSubscriptionUpdateParams{Enabled: stripe.Bool(false)},
			},
		}
		keyed(params, "portal-configuration-"+portalConfigurationName)
		created, err := s.client.V1BillingPortalConfigurations.Create(ctx, params)
		if err != nil {
			return "", fmt.Errorf("create portal configuration: %w", err)
		}
		id = created.ID
	}
	s.mu.Lock()
	s.portal = id
	s.mu.Unlock()
	return id, nil
}

type checkout struct {
	id      string
	url     string
	expires time.Time
}

func (s *stripeProvider) creditCheckout(ctx context.Context, customer string, purchase uuid.UUID, cents int64, success, cancel string) (checkout, error) {
	params := &stripe.CheckoutSessionCreateParams{
		Mode: stripe.String(string(stripe.CheckoutSessionModePayment)), Customer: stripe.String(customer),
		ClientReferenceID: stripe.String(purchase.String()),
		Metadata:          map[string]string{"credit_purchase_id": purchase.String()},
		PaymentIntentData: &stripe.CheckoutSessionCreatePaymentIntentDataParams{
			Metadata: map[string]string{"credit_purchase_id": purchase.String()},
		},
		LineItems: []*stripe.CheckoutSessionCreateLineItemParams{{
			Quantity: stripe.Int64(1),
			PriceData: &stripe.CheckoutSessionCreateLineItemPriceDataParams{
				Currency: stripe.String(strings.ToLower(Currency)), UnitAmount: stripe.Int64(cents),
				ProductData: &stripe.CheckoutSessionCreateLineItemPriceDataProductDataParams{Name: stripe.String("Compute and storage credit")},
			},
		}},
		SuccessURL: stripe.String(success), CancelURL: stripe.String(cancel),
	}
	params.AddExtra("payment_method_types[0]", "card")
	keyed(params, "credit-checkout-"+purchase.String())
	session, err := s.client.V1CheckoutSessions.Create(ctx, params)
	if err != nil {
		return checkout{}, fmt.Errorf("create credit checkout: %w", err)
	}
	return checkout{id: session.ID, url: session.URL, expires: time.Unix(session.ExpiresAt, 0).UTC()}, nil
}

// payment is what Stripe holds of one credit payment.
type payment struct {
	intent   string
	customer string
	purchase string
	status   string
	amount   int64
	// retained is what the platform keeps: received less refunds and open
	// or lost disputes, in cents.
	retained int64
}

const (
	paymentPending        = "pending"
	paymentActionRequired = "action_required"
	paymentSucceeded      = "succeeded"
	paymentDeclined       = "declined"
	paymentCancelled      = "cancelled"
)

func (s *stripeProvider) payment(ctx context.Context, intentID string) (payment, error) {
	params := &stripe.PaymentIntentRetrieveParams{}
	params.AddExpand("latest_charge")
	intent, err := s.client.V1PaymentIntents.Retrieve(ctx, intentID, params)
	if err != nil {
		return payment{}, fmt.Errorf("read payment %s: %w", intentID, err)
	}
	return s.paymentOf(ctx, intent)
}

func (s *stripeProvider) paymentOf(ctx context.Context, intent *stripe.PaymentIntent) (payment, error) {
	p := payment{intent: intent.ID, purchase: intent.Metadata["credit_purchase_id"], amount: intent.Amount}
	if intent.Customer != nil {
		p.customer = intent.Customer.ID
	}
	switch intent.Status {
	case stripe.PaymentIntentStatusSucceeded:
		p.status = paymentSucceeded
	case stripe.PaymentIntentStatusCanceled:
		p.status = paymentCancelled
	case stripe.PaymentIntentStatusRequiresAction:
		p.status = paymentActionRequired
	case stripe.PaymentIntentStatusRequiresPaymentMethod:
		p.status = paymentDeclined
		if intent.LastPaymentError != nil && intent.LastPaymentError.Code == stripe.ErrorCodeAuthenticationRequired {
			p.status = paymentActionRequired
		}
	case stripe.PaymentIntentStatusProcessing, stripe.PaymentIntentStatusRequiresCapture, stripe.PaymentIntentStatusRequiresConfirmation:
		p.status = paymentPending
	default:
		return payment{}, fmt.Errorf("payment %s has status %q", intent.ID, intent.Status)
	}
	p.retained = intent.AmountReceived
	if charge := intent.LatestCharge; charge != nil {
		p.retained -= charge.AmountRefunded
		if charge.Disputed {
			disputed, err := s.disputed(ctx, charge.ID)
			if err != nil {
				return payment{}, err
			}
			p.retained -= disputed
		}
	}
	p.retained = max(0, p.retained)
	return p, nil
}

// disputed is the amount of a charge's disputes that are open or lost.
func (s *stripeProvider) disputed(ctx context.Context, charge string) (int64, error) {
	var total int64
	for d, err := range s.client.V1Disputes.List(ctx, &stripe.DisputeListParams{Charge: stripe.String(charge)}).All(ctx) {
		if err != nil {
			return 0, fmt.Errorf("list disputes of %s: %w", charge, err)
		}
		switch d.Status {
		case stripe.DisputeStatusNeedsResponse, stripe.DisputeStatusUnderReview, stripe.DisputeStatusLost:
			total += d.Amount
		case stripe.DisputeStatusWon, stripe.DisputeStatusWarningClosed, stripe.DisputeStatusWarningNeedsResponse,
			stripe.DisputeStatusWarningUnderReview, stripe.DisputeStatusPrevented:
		}
	}
	return total, nil
}

// paymentOfPurchase finds the payment carrying a purchase's id, or "" when
// none exists.
func (s *stripeProvider) paymentOfPurchase(ctx context.Context, purchase uuid.UUID) (string, error) {
	search := &stripe.PaymentIntentSearchParams{SearchParams: stripe.SearchParams{
		Query: fmt.Sprintf("metadata['credit_purchase_id']:'%s'", purchase),
	}}
	for intent, err := range s.client.V1PaymentIntents.Search(ctx, search).All(ctx) {
		if err != nil {
			return "", fmt.Errorf("search payments of %s: %w", purchase, err)
		}
		if intent.Metadata["credit_purchase_id"] == purchase.String() {
			return intent.ID, nil
		}
	}
	return "", nil
}

// checkoutPayment returns the payment a credit checkout collected, or the
// session's own outcome when it collected none.
func (s *stripeProvider) checkoutPayment(ctx context.Context, sessionID string) (payment, error) {
	session, err := s.client.V1CheckoutSessions.Retrieve(ctx, sessionID, nil)
	if err != nil {
		return payment{}, fmt.Errorf("read checkout %s: %w", sessionID, err)
	}
	if session.PaymentIntent != nil && session.PaymentIntent.ID != "" {
		return s.payment(ctx, session.PaymentIntent.ID)
	}
	p := payment{purchase: session.Metadata["credit_purchase_id"], status: paymentPending}
	if session.Status == stripe.CheckoutSessionStatusExpired {
		p.status = paymentCancelled
	}
	return p, nil
}

// chargeCard charges the customer's default card off-session. A decline or
// a card that needs authentication is an outcome, not an error.
func (s *stripeProvider) chargeCard(ctx context.Context, customer string, purchase uuid.UUID, cents int64) (payment, error) {
	cust, err := s.client.V1Customers.Retrieve(ctx, customer, nil)
	if err != nil {
		return payment{}, fmt.Errorf("read customer %s: %w", customer, err)
	}
	if cust.InvoiceSettings == nil || cust.InvoiceSettings.DefaultPaymentMethod == nil {
		return payment{purchase: purchase.String(), status: paymentDeclined}, nil
	}
	params := &stripe.PaymentIntentCreateParams{
		Customer: stripe.String(customer), Amount: stripe.Int64(cents), Currency: stripe.String(strings.ToLower(Currency)),
		PaymentMethod: stripe.String(cust.InvoiceSettings.DefaultPaymentMethod.ID), Confirm: stripe.Bool(true),
		OffSession: stripe.Bool(true), Metadata: map[string]string{"credit_purchase_id": purchase.String()},
	}
	params.AddExtra("payment_method_types[0]", "card")
	params.AddExpand("latest_charge")
	keyed(params, "credit-payment-"+purchase.String())
	intent, err := s.client.V1PaymentIntents.Create(ctx, params)
	if se, ok := refusal(err); ok && se.Type == stripe.ErrorTypeCard {
		status := paymentDeclined
		if se.Code == stripe.ErrorCodeAuthenticationRequired {
			status = paymentActionRequired
		}
		out := payment{purchase: purchase.String(), status: status}
		if se.PaymentIntent != nil {
			out.intent = se.PaymentIntent.ID
		}
		return out, nil
	}
	if err != nil {
		return payment{}, fmt.Errorf("charge saved card: %w", err)
	}
	return s.paymentOf(ctx, intent)
}

// card is a customer's saved card state.
type card struct {
	customer string
	present  bool
}

// syncCard makes sure a customer with a saved card has a default one,
// promoting the newest card when the default was removed, and reports
// whether one exists.
func (s *stripeProvider) syncCard(ctx context.Context, customer string) (card, error) {
	cust, err := s.client.V1Customers.Retrieve(ctx, customer, nil)
	if err != nil {
		return card{}, fmt.Errorf("read customer %s: %w", customer, err)
	}
	if cust.Deleted {
		return card{customer: customer}, nil
	}
	if cust.InvoiceSettings != nil && cust.InvoiceSettings.DefaultPaymentMethod != nil && cust.InvoiceSettings.DefaultPaymentMethod.ID != "" {
		return card{customer: customer, present: true}, nil
	}
	list := s.client.V1Customers.ListPaymentMethods(ctx, &stripe.CustomerListPaymentMethodsParams{
		Customer: stripe.String(customer), Type: stripe.String("card"), ListParams: stripe.ListParams{Limit: stripe.Int64(1)},
	})
	for method, err := range list.All(ctx) {
		if err != nil {
			return card{}, fmt.Errorf("list cards of %s: %w", customer, err)
		}
		if _, err := s.client.V1Customers.Update(ctx, customer, &stripe.CustomerUpdateParams{
			InvoiceSettings: &stripe.CustomerUpdateInvoiceSettingsParams{DefaultPaymentMethod: stripe.String(method.ID)},
		}); err != nil {
			return card{}, fmt.Errorf("set default card of %s: %w", customer, err)
		}
		return card{customer: customer, present: true}, nil
	}
	return card{customer: customer}, nil
}

// price returns the Stripe price of paid terms, creating its product and
// price when the account has none under the lookup key.
func (s *stripeProvider) price(ctx context.Context, v TermsVersion) (string, error) {
	s.mu.Lock()
	id, ok := s.prices[v]
	s.mu.Unlock()
	if ok {
		return id, nil
	}
	plan, err := planOfTerms(v)
	if err != nil {
		return "", err
	}
	key := priceLookupKey(v)
	for p, err := range s.client.V1Prices.List(ctx, &stripe.PriceListParams{LookupKeys: []*string{stripe.String(key)}, Active: stripe.Bool(true)}).All(ctx) {
		if err != nil {
			return "", fmt.Errorf("find price %s: %w", key, err)
		}
		id = p.ID
		break
	}
	if id == "" {
		product := &stripe.ProductCreateParams{Name: stripe.String("LazyCloud " + plan.Name)}
		keyed(product, "product-"+key)
		created, err := s.client.V1Products.Create(ctx, product)
		if err != nil {
			return "", fmt.Errorf("create product %s: %w", key, err)
		}
		params := &stripe.PriceCreateParams{
			Product: stripe.String(created.ID), Currency: stripe.String(strings.ToLower(Currency)),
			UnitAmount: stripe.Int64(plan.MonthlyNanos / nanosPerCent), LookupKey: stripe.String(key),
			Recurring: &stripe.PriceCreateRecurringParams{Interval: stripe.String(string(stripe.PriceRecurringIntervalMonth))},
		}
		keyed(params, "price-"+key)
		price, err := s.client.V1Prices.Create(ctx, params)
		if err != nil {
			return "", fmt.Errorf("create price %s: %w", key, err)
		}
		id = price.ID
	}
	s.mu.Lock()
	s.prices[v] = id
	s.mu.Unlock()
	return id, nil
}

// termsOfPrice maps a Stripe price back to the paid terms it sells, by
// lookup key.
func termsOfPrice(p *stripe.Price) (TermsVersion, bool) {
	if p == nil {
		return "", false
	}
	for _, v := range []TermsVersion{TermsTeam, TermsBusiness} {
		if p.LookupKey == priceLookupKey(v) {
			return v, true
		}
	}
	return "", false
}

// subscription is the account state a Stripe subscription implies.
type subscription struct {
	id          string
	customer    string
	status      stripe.SubscriptionStatus
	terms       TermsVersion
	item        string
	periodStart time.Time
	periodEnd   time.Time
	scheduled   *TermsVersion
	scheduledAt *time.Time
	schedule    string
	cancelAtEnd bool
	invoice     string
}

func (s *stripeProvider) subscription(ctx context.Context, id string) (subscription, error) {
	params := &stripe.SubscriptionRetrieveParams{}
	params.AddExpand("items.data.price")
	params.AddExpand("schedule")
	sub, err := s.client.V1Subscriptions.Retrieve(ctx, id, params)
	if err != nil {
		return subscription{}, fmt.Errorf("read subscription %s: %w", id, err)
	}
	return s.subscriptionOf(ctx, sub)
}

func (s *stripeProvider) subscriptionOf(ctx context.Context, sub *stripe.Subscription) (subscription, error) {
	out := subscription{id: sub.ID, status: sub.Status, cancelAtEnd: sub.CancelAtPeriodEnd}
	if sub.Customer != nil {
		out.customer = sub.Customer.ID
	}
	if sub.LatestInvoice != nil {
		out.invoice = sub.LatestInvoice.ID
	}
	if sub.Items != nil {
		for _, item := range sub.Items.Data {
			if v, ok := termsOfPrice(item.Price); ok {
				out.terms, out.item = v, item.ID
				out.periodStart = time.Unix(item.CurrentPeriodStart, 0).UTC()
				out.periodEnd = time.Unix(item.CurrentPeriodEnd, 0).UTC()
			}
		}
	}
	if out.terms == "" {
		return subscription{}, fmt.Errorf("subscription %s holds no published plan price", sub.ID)
	}
	if out.cancelAtEnd {
		free, at := TermsFree, out.periodEnd
		out.scheduled, out.scheduledAt = &free, &at
	}
	if sub.Schedule != nil && sub.Schedule.ID != "" {
		out.schedule = sub.Schedule.ID
		params := &stripe.SubscriptionScheduleRetrieveParams{}
		params.AddExpand("phases.items.price")
		schedule, err := s.client.V1SubscriptionSchedules.Retrieve(ctx, sub.Schedule.ID, params)
		if err != nil {
			return subscription{}, fmt.Errorf("read schedule %s: %w", sub.Schedule.ID, err)
		}
		for _, phase := range schedule.Phases {
			if phase.StartDate != out.periodEnd.Unix() {
				continue
			}
			for _, item := range phase.Items {
				if v, ok := termsOfPrice(item.Price); ok && v != out.terms {
					at := out.periodEnd
					out.scheduled, out.scheduledAt = &v, &at
				}
			}
		}
	}
	return out, nil
}

// subscribe starts a paid subscription charged now to the default card.
func (s *stripeProvider) subscribe(ctx context.Context, customer string, v TermsVersion, key string) (subscription, error) {
	price, err := s.price(ctx, v)
	if err != nil {
		return subscription{}, err
	}
	params := &stripe.SubscriptionCreateParams{
		Customer: stripe.String(customer), Items: []*stripe.SubscriptionCreateItemParams{{Price: stripe.String(price)}},
		PaymentBehavior: stripe.String("error_if_incomplete"),
	}
	params.AddExpand("items.data.price")
	keyed(params, key)
	sub, err := s.client.V1Subscriptions.Create(ctx, params)
	if err != nil {
		return subscription{}, fmt.Errorf("create subscription: %w", err)
	}
	return s.subscriptionOf(ctx, sub)
}

// upgrade moves a subscription onto dearer terms now, invoicing the
// prorated difference at once.
func (s *stripeProvider) upgrade(ctx context.Context, current subscription, v TermsVersion, key string) (subscription, error) {
	if err := s.release(ctx, current, key); err != nil {
		return subscription{}, err
	}
	price, err := s.price(ctx, v)
	if err != nil {
		return subscription{}, err
	}
	params := &stripe.SubscriptionUpdateParams{
		Items:             []*stripe.SubscriptionUpdateItemParams{{ID: stripe.String(current.item), Price: stripe.String(price)}},
		ProrationBehavior: stripe.String("always_invoice"), PaymentBehavior: stripe.String("error_if_incomplete"),
		CancelAtPeriodEnd: stripe.Bool(false),
	}
	params.AddExpand("items.data.price")
	keyed(params, key)
	sub, err := s.client.V1Subscriptions.Update(ctx, current.id, params)
	if err != nil {
		return subscription{}, fmt.Errorf("upgrade subscription: %w", err)
	}
	return s.subscriptionOf(ctx, sub)
}

// scheduleDowngrade moves a subscription onto cheaper paid terms at
// renewal through a subscription schedule; Free ends it at renewal.
func (s *stripeProvider) scheduleDowngrade(ctx context.Context, current subscription, v TermsVersion, key string) (subscription, error) {
	if v == TermsFree {
		if err := s.release(ctx, current, key); err != nil {
			return subscription{}, err
		}
		params := &stripe.SubscriptionUpdateParams{CancelAtPeriodEnd: stripe.Bool(true)}
		keyed(params, key+"-cancel")
		if _, err := s.client.V1Subscriptions.Update(ctx, current.id, params); err != nil {
			return subscription{}, fmt.Errorf("end subscription at renewal: %w", err)
		}
		return s.subscription(ctx, current.id)
	}
	if current.cancelAtEnd {
		params := &stripe.SubscriptionUpdateParams{CancelAtPeriodEnd: stripe.Bool(false)}
		keyed(params, key+"-keep")
		if _, err := s.client.V1Subscriptions.Update(ctx, current.id, params); err != nil {
			return subscription{}, fmt.Errorf("keep subscription: %w", err)
		}
	}
	scheduleID := current.schedule
	if scheduleID == "" {
		params := &stripe.SubscriptionScheduleCreateParams{FromSubscription: stripe.String(current.id)}
		keyed(params, key+"-schedule")
		created, err := s.client.V1SubscriptionSchedules.Create(ctx, params)
		if err != nil {
			return subscription{}, fmt.Errorf("create subscription schedule: %w", err)
		}
		scheduleID = created.ID
	}
	currentPrice, err := s.price(ctx, current.terms)
	if err != nil {
		return subscription{}, err
	}
	nextPrice, err := s.price(ctx, v)
	if err != nil {
		return subscription{}, err
	}
	params := &stripe.SubscriptionScheduleUpdateParams{
		EndBehavior: stripe.String("release"), ProrationBehavior: stripe.String("none"),
		Phases: []*stripe.SubscriptionScheduleUpdatePhaseParams{
			{
				Items:     []*stripe.SubscriptionScheduleUpdatePhaseItemParams{{Price: stripe.String(currentPrice)}},
				StartDate: stripe.Int64(current.periodStart.Unix()), EndDate: stripe.Int64(current.periodEnd.Unix()),
				ProrationBehavior: stripe.String("none"),
			},
			{
				Items:    []*stripe.SubscriptionScheduleUpdatePhaseItemParams{{Price: stripe.String(nextPrice)}},
				Duration: &stripe.SubscriptionScheduleUpdatePhaseDurationParams{Interval: stripe.String("month"), IntervalCount: stripe.Int64(1)},
			},
		},
	}
	keyed(params, key+"-phases")
	if _, err := s.client.V1SubscriptionSchedules.Update(ctx, scheduleID, params); err != nil {
		return subscription{}, fmt.Errorf("schedule plan change: %w", err)
	}
	return s.subscription(ctx, current.id)
}

// keep cancels a scheduled change, so the subscription renews on its
// current terms.
func (s *stripeProvider) keep(ctx context.Context, current subscription, key string) (subscription, error) {
	if err := s.release(ctx, current, key); err != nil {
		return subscription{}, err
	}
	if current.cancelAtEnd {
		params := &stripe.SubscriptionUpdateParams{CancelAtPeriodEnd: stripe.Bool(false)}
		keyed(params, key+"-keep")
		if _, err := s.client.V1Subscriptions.Update(ctx, current.id, params); err != nil {
			return subscription{}, fmt.Errorf("keep subscription: %w", err)
		}
	}
	return s.subscription(ctx, current.id)
}

// release detaches the subscription's schedule, dropping a scheduled
// change.
func (s *stripeProvider) release(ctx context.Context, current subscription, key string) error {
	if current.schedule == "" {
		return nil
	}
	params := &stripe.SubscriptionScheduleReleaseParams{}
	keyed(params, key+"-release")
	if _, err := s.client.V1SubscriptionSchedules.Release(ctx, current.schedule, params); err != nil {
		if se, ok := refusal(err); ok && se.Type == stripe.ErrorTypeInvalidRequest {
			// Already released or ended.
			return nil
		}
		return fmt.Errorf("release subscription schedule: %w", err)
	}
	return nil
}

// invoiceLine is one subscription line of a paid invoice.
type invoiceLine struct {
	id          string
	terms       TermsVersion
	amount      int64
	proration   bool
	periodStart time.Time
	periodEnd   time.Time
}

type invoice struct {
	id           string
	customer     string
	subscription string
	paid         bool
	paidAt       time.Time
	lines        []invoiceLine
}

func (s *stripeProvider) invoice(ctx context.Context, id string) (invoice, error) {
	inv, err := s.client.V1Invoices.Retrieve(ctx, id, nil)
	if err != nil {
		return invoice{}, fmt.Errorf("read invoice %s: %w", id, err)
	}
	out := invoice{id: inv.ID, paid: inv.Status == stripe.InvoiceStatusPaid}
	if inv.Customer != nil {
		out.customer = inv.Customer.ID
	}
	if inv.Parent != nil && inv.Parent.SubscriptionDetails != nil && inv.Parent.SubscriptionDetails.Subscription != nil {
		out.subscription = inv.Parent.SubscriptionDetails.Subscription.ID
	}
	if inv.StatusTransitions != nil && inv.StatusTransitions.PaidAt > 0 {
		out.paidAt = time.Unix(inv.StatusTransitions.PaidAt, 0).UTC()
	}
	params := &stripe.InvoiceListLinesParams{Invoice: stripe.String(id)}
	params.AddExpand("data.pricing.price_details.price")
	for line, err := range s.client.V1Invoices.ListLines(ctx, params).All(ctx) {
		if err != nil {
			return invoice{}, fmt.Errorf("list lines of %s: %w", id, err)
		}
		if line.Pricing == nil || line.Pricing.PriceDetails == nil || line.Period == nil {
			continue
		}
		v, ok := termsOfPrice(line.Pricing.PriceDetails.Price)
		if !ok {
			continue
		}
		l := invoiceLine{
			id: line.ID, terms: v, amount: line.Amount,
			periodStart: time.Unix(line.Period.Start, 0).UTC(), periodEnd: time.Unix(line.Period.End, 0).UTC(),
		}
		if line.Parent != nil && line.Parent.SubscriptionItemDetails != nil {
			l.proration = line.Parent.SubscriptionItemDetails.Proration
		}
		out.lines = append(out.lines, l)
	}
	return out, nil
}
