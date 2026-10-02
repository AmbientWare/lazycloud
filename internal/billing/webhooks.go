package billing

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/stripe/stripe-go/v87"
	"github.com/stripe/stripe-go/v87/webhook"
)

const (
	eventBatch = 50
	// eventAttempts bounds retries of one delivery; with the backoff below
	// they span about a day.
	eventAttempts  = 20
	eventRetention = 30 * 24 * time.Hour
	eventPurge     = 1000
)

// ErrBadSignature means a webhook delivery is not signed with the
// endpoint's secret.
var ErrBadSignature = errors.New("the delivery is not signed by Stripe")

// ReceiveWebhook verifies a Stripe delivery and stores it for processing.
// Nothing in it is believed beyond which object changed: processing fetches
// the object again.
func (b *Billing) ReceiveWebhook(ctx context.Context, payload []byte, signature string) error {
	if b.stripe == nil || b.stripe.webhookSecret == "" {
		return ErrPaymentsUnavailable
	}
	event, err := webhook.ConstructEventWithOptions(payload, signature, b.stripe.webhookSecret,
		webhook.ConstructEventOptions{IgnoreAPIVersionMismatch: true})
	if err != nil {
		return fmt.Errorf("%w: %w", ErrBadSignature, err)
	}
	if event.Data == nil {
		return &InvalidError{Message: "the delivery names no object"}
	}
	object := event.GetObjectValue("id")
	customer := event.GetObjectValue("customer")
	switch {
	case customer == "":
		customer = event.GetPreviousValue("customer")
	case strings.HasPrefix(string(event.Type), "customer.") && !strings.HasPrefix(string(event.Type), "customer.subscription."):
		customer = object
	}
	params := StoreEventParams{ID: event.ID, Type: string(event.Type), ObjectID: object}
	if customer != "" {
		params.CustomerID = &customer
	}
	if err := b.queries.StoreEvent(ctx, params); err != nil {
		return fmt.Errorf("store stripe event: %w", err)
	}
	return nil
}

// ProcessEvents handles stored deliveries: each fetches the object it
// names and settles what it says about purchases, cards and subscriptions.
// A delivery that fails is retried with backoff and dropped after
// eventAttempts.
func (b *Billing) ProcessEvents(ctx context.Context) (int, error) {
	if b.stripe == nil {
		return 0, nil
	}
	events, err := b.queries.ClaimEvents(ctx, eventBatch)
	if err != nil {
		return 0, fmt.Errorf("claim stripe events: %w", err)
	}
	for _, e := range events {
		customer := ""
		if e.CustomerID != nil {
			customer = *e.CustomerID
		}
		err := b.processEvent(ctx, e.Type, e.ObjectID, customer)
		switch {
		case err == nil:
			err = b.queries.FinishEvent(ctx, FinishEventParams{ID: e.ID})
		case e.Attempts >= eventAttempts:
			b.logger.ErrorContext(ctx, "stripe event dropped", "event", e.ID, "type", e.Type, "error", err)
			err = b.queries.FinishEvent(ctx, FinishEventParams{ID: e.ID, LastError: truncate(err.Error())})
		default:
			b.logger.WarnContext(ctx, "stripe event deferred", "event", e.ID, "type", e.Type, "attempt", e.Attempts, "error", err)
			next := time.Now().Add(min(10*time.Second<<min(e.Attempts, 13), 2*time.Hour))
			err = b.queries.RetryEvent(ctx, RetryEventParams{ID: e.ID, NextAttemptAt: next, LastError: truncate(err.Error())})
		}
		if err != nil {
			return 0, fmt.Errorf("settle stripe event %s: %w", e.ID, err)
		}
	}
	if _, err := b.queries.PurgeEvents(ctx, PurgeEventsParams{Before: time.Now().Add(-eventRetention), RowLimit: eventPurge}); err != nil {
		return len(events), fmt.Errorf("purge stripe events: %w", err)
	}
	return len(events), nil
}

func (b *Billing) processEvent(ctx context.Context, kind, object, customer string) error {
	s := b.stripe
	switch {
	case strings.HasPrefix(kind, "checkout.session."):
		session, err := s.client.V1CheckoutSessions.Retrieve(ctx, object, nil)
		if err != nil {
			return fmt.Errorf("read checkout %s: %w", object, err)
		}
		if session.Mode == stripe.CheckoutSessionModeSetup {
			if session.SetupIntent == nil || session.Customer == nil {
				return nil
			}
			return b.savedCard(ctx, session.Customer.ID, session.SetupIntent.ID)
		}
		return b.settlePurchaseBy(ctx, func() (uuid.UUID, error) { return b.queries.PurchaseByCheckout(ctx, object) })
	case kind == "setup_intent.succeeded":
		intent, err := s.client.V1SetupIntents.Retrieve(ctx, object, nil)
		if err != nil {
			return fmt.Errorf("read setup intent %s: %w", object, err)
		}
		if intent.Customer == nil || intent.PaymentMethod == nil {
			return nil
		}
		return b.makeDefault(ctx, intent.Customer.ID, intent.PaymentMethod.ID)
	case kind == "payment_method.attached":
		return b.makeDefault(ctx, customer, object)
	case kind == "payment_method.detached", kind == "customer.updated":
		return b.refreshCard(ctx, customer)
	case strings.HasPrefix(kind, "payment_intent."):
		return b.settlePurchaseBy(ctx, func() (uuid.UUID, error) {
			id, err := b.queries.PurchaseByIntent(ctx, object)
			if !errors.Is(err, pgx.ErrNoRows) {
				return id, err
			}
			// A payment whose creation response was lost names its purchase.
			intent, err := s.client.V1PaymentIntents.Retrieve(ctx, object, nil)
			if err != nil {
				return uuid.UUID{}, fmt.Errorf("read payment %s: %w", object, err)
			}
			purchase, err := uuid.Parse(intent.Metadata["credit_purchase_id"])
			if err != nil {
				return uuid.UUID{}, pgx.ErrNoRows
			}
			if err := b.queries.PurchaseIntentFound(ctx, PurchaseIntentFoundParams{ID: purchase, PaymentIntentID: object}); err != nil {
				return uuid.UUID{}, fmt.Errorf("record found payment: %w", err)
			}
			return purchase, nil
		})
	case strings.HasPrefix(kind, "charge.dispute."):
		dispute, err := s.client.V1Disputes.Retrieve(ctx, object, nil)
		if err != nil {
			return fmt.Errorf("read dispute %s: %w", object, err)
		}
		if dispute.PaymentIntent == nil {
			return nil
		}
		return b.settlePurchaseBy(ctx, func() (uuid.UUID, error) { return b.queries.PurchaseByIntent(ctx, dispute.PaymentIntent.ID) })
	case kind == "charge.refunded", kind == "charge.refund.updated":
		charge, err := s.client.V1Charges.Retrieve(ctx, object, nil)
		if err != nil {
			return fmt.Errorf("read charge %s: %w", object, err)
		}
		if charge.PaymentIntent == nil {
			return nil
		}
		return b.settlePurchaseBy(ctx, func() (uuid.UUID, error) { return b.queries.PurchaseByIntent(ctx, charge.PaymentIntent.ID) })
	case strings.HasPrefix(kind, "customer.subscription."):
		return b.syncSubscription(ctx, object)
	case kind == "invoice.paid", kind == "invoice.payment_failed", kind == "invoice.payment_succeeded":
		inv, err := s.invoice(ctx, object)
		if err != nil {
			return err
		}
		if inv.subscription == "" {
			return nil
		}
		if err := b.syncSubscription(ctx, inv.subscription); err != nil {
			return err
		}
		return b.creditInvoice(ctx, object)
	}
	return nil
}

// settlePurchaseBy settles the purchase a lookup found; a payment that is
// not a credit purchase changes nothing.
func (b *Billing) settlePurchaseBy(ctx context.Context, find func() (uuid.UUID, error)) error {
	id, err := find()
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("find purchase: %w", err)
	}
	return b.settlePurchase(ctx, id)
}

// savedCard makes the card a setup checkout collected the customer's
// default.
func (b *Billing) savedCard(ctx context.Context, customer, setupIntent string) error {
	intent, err := b.stripe.client.V1SetupIntents.Retrieve(ctx, setupIntent, nil)
	if err != nil {
		return fmt.Errorf("read setup intent %s: %w", setupIntent, err)
	}
	if intent.PaymentMethod == nil {
		return nil
	}
	return b.makeDefault(ctx, customer, intent.PaymentMethod.ID)
}

// makeDefault makes a newly saved card the one charges are taken from,
// when it still belongs to the customer: deliveries are retried for days,
// and one about a card since replaced must not put it back.
func (b *Billing) makeDefault(ctx context.Context, customer, method string) error {
	if customer == "" {
		return nil
	}
	if _, err := b.queries.AccountByCustomer(ctx, customer); errors.Is(err, pgx.ErrNoRows) {
		return nil
	} else if err != nil {
		return fmt.Errorf("find account of %s: %w", customer, err)
	}
	pm, err := b.stripe.client.V1PaymentMethods.Retrieve(ctx, method, nil)
	if err != nil {
		return fmt.Errorf("read payment method %s: %w", method, err)
	}
	if pm.Customer != nil && pm.Customer.ID == customer {
		if _, err := b.stripe.client.V1Customers.Update(ctx, customer, &stripe.CustomerUpdateParams{
			InvoiceSettings: &stripe.CustomerUpdateInvoiceSettingsParams{DefaultPaymentMethod: stripe.String(method)},
		}); err != nil {
			return fmt.Errorf("set default card of %s: %w", customer, err)
		}
	}
	return b.refreshCard(ctx, customer)
}

// refreshCard records whether the customer has a card to charge, promoting
// another saved card when the default was removed.
func (b *Billing) refreshCard(ctx context.Context, customer string) error {
	if customer == "" {
		return nil
	}
	user, err := b.queries.AccountByCustomer(ctx, customer)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("find account of %s: %w", customer, err)
	}
	c, err := b.stripe.syncCard(ctx, customer)
	if err != nil {
		return err
	}
	if err := b.queries.SetCard(ctx, SetCardParams{UserID: user, Present: c.present}); err != nil {
		return fmt.Errorf("record card: %w", err)
	}
	return nil
}
