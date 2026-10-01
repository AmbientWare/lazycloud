package billing

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

const (
	purchaseManual    = "manual"
	purchaseAutomatic = "automatic"
	// purchaseRetryWindow is how long a purchase whose Stripe call never
	// returned is retried under its idempotency key, which Stripe keeps for
	// 24 hours.
	purchaseRetryWindow = 20 * time.Hour
	paymentSweepBatch   = 50
	reloadBatch         = 20
	// settledRecheck is when a succeeded purchase is read again for refunds
	// and disputes if no delivery reports them first.
	settledRecheck = 30 * 24 * time.Hour
)

// ownURL refuses to send anyone anywhere but back to the dashboard: Stripe
// redirects to whatever it is given, right after money moved.
func (b *Billing) ownURL(candidate string) (string, error) {
	u, err := url.Parse(candidate)
	if err != nil {
		return "", &InvalidError{Message: "a billing return address must be a valid URL"}
	}
	own, err := url.Parse(b.cfg.PublicURL)
	if err != nil || u.Scheme != own.Scheme || u.Host != own.Host {
		return "", &InvalidError{Message: "a billing return address must be on this platform"}
	}
	return candidate, nil
}

func (b *Billing) returnURLs(returnURL string, cancelURL *string) (string, string, error) {
	success, err := b.ownURL(returnURL)
	if err != nil {
		return "", "", err
	}
	cancel := success
	if cancelURL != nil && *cancelURL != "" {
		if cancel, err = b.ownURL(*cancelURL); err != nil {
			return "", "", err
		}
	}
	return success, cancel, nil
}

func (b *Billing) payments() (*stripeProvider, error) {
	if b.stripe == nil {
		return nil, ErrPaymentsUnavailable
	}
	return b.stripe, nil
}

// customerFor returns the account's Stripe customer, creating it on first
// need. The account row exists first, so its id keys the creation.
func (b *Billing) customerFor(ctx context.Context, user uuid.UUID) (string, error) {
	s, err := b.payments()
	if err != nil {
		return "", err
	}
	var row CustomerOfRow
	err = pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		if err := ensureAccount(ctx, q, user); err != nil {
			return err
		}
		row, err = q.CustomerOf(ctx, user)
		return err
	})
	if err != nil {
		return "", fmt.Errorf("read billing customer: %w", err)
	}
	if row.StripeCustomerID != nil {
		return *row.StripeCustomerID, nil
	}
	email := ""
	if row.Email != nil {
		email = *row.Email
	}
	customer, err := s.customer(ctx, user, email)
	if err != nil {
		return "", err
	}
	recorded, err := b.queries.SetCustomer(ctx, SetCustomerParams{UserID: user, Customer: customer})
	if err != nil {
		return "", fmt.Errorf("record stripe customer: %w", err)
	}
	return recorded, nil
}

// PaymentMethodSession opens Stripe's page for saving a card. The card
// becomes the account's when Stripe's delivery reports it.
func (b *Billing) PaymentMethodSession(ctx context.Context, user uuid.UUID, req apitypes.HostedSessionRequest) (string, error) {
	success, cancel, err := b.returnURLs(req.ReturnUrl, req.CancelUrl)
	if err != nil {
		return "", err
	}
	customer, err := b.customerFor(ctx, user)
	if err != nil {
		return "", err
	}
	return b.stripe.setupSession(ctx, customer, success, cancel)
}

// PortalSession opens Stripe's page for invoices and the saved card.
func (b *Billing) PortalSession(ctx context.Context, user uuid.UUID, req apitypes.HostedSessionRequest) (string, error) {
	s, err := b.payments()
	if err != nil {
		return "", err
	}
	returnURL, err := b.ownURL(req.ReturnUrl)
	if err != nil {
		return "", err
	}
	row, err := b.queries.CustomerOf(ctx, user)
	if errors.Is(err, pgx.ErrNoRows) || (err == nil && row.StripeCustomerID == nil) {
		return "", ErrNotFound
	}
	if err != nil {
		return "", fmt.Errorf("read billing customer: %w", err)
	}
	return s.portalSession(ctx, *row.StripeCustomerID, returnURL)
}

// BuyCredit starts a prepaid credit purchase through Stripe Checkout. The
// purchase row is committed first and keys the checkout, and a repeated
// request key returns the same purchase.
func (b *Billing) BuyCredit(ctx context.Context, user uuid.UUID, req apitypes.CreditPurchaseRequest) (apitypes.CreditPurchase, error) {
	s, err := b.payments()
	if err != nil {
		return apitypes.CreditPurchase{}, err
	}
	success, cancel, err := b.returnURLs(req.ReturnUrl, req.CancelUrl)
	if err != nil {
		return apitypes.CreditPurchase{}, err
	}
	amount := int64(req.AmountCents) * nanosPerCent
	var purchase CreditPurchase
	var customer string
	err = pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		if err := ensureAccount(ctx, q, user); err != nil {
			return err
		}
		account, err := q.LockAccount(ctx, user)
		if err != nil {
			return fmt.Errorf("lock account: %w", err)
		}
		if account.PaymentMethodAttachedAt == nil || account.StripeCustomerID == nil {
			return &PaymentRequiredError{Message: "add a payment method before buying credit"}
		}
		customer = *account.StripeCustomerID
		key := req.RequestKey
		_, err = q.InsertManualPurchase(ctx, InsertManualPurchaseParams{
			UserID: user, RequestKey: &key, AmountNanos: amount, SuccessUrl: success, CancelUrl: cancel,
		})
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			return fmt.Errorf("insert purchase: %w", err)
		}
		purchase, err = q.PurchaseByRequest(ctx, PurchaseByRequestParams{UserID: user, RequestKey: &key})
		if err != nil {
			return fmt.Errorf("read purchase: %w", err)
		}
		if purchase.AmountNanos != amount || purchase.SuccessUrl != success || purchase.CancelUrl != cancel {
			return &ConflictError{Message: "a purchase request cannot be reused with different terms"}
		}
		return nil
	})
	if err != nil {
		return apitypes.CreditPurchase{}, fmt.Errorf("buy credit: %w", err)
	}
	if purchase.CheckoutSessionID == nil && purchase.Status == paymentPending {
		session, err := s.creditCheckout(ctx, customer, purchase.ID, purchase.AmountNanos/nanosPerCent, success, cancel)
		if err != nil {
			return apitypes.CreditPurchase{}, err
		}
		if err := b.queries.SetCheckout(ctx, SetCheckoutParams{
			ID: purchase.ID, CheckoutSessionID: &session.id, CheckoutUrl: &session.url, CheckoutExpiresAt: &session.expires,
			NextAttemptAt: session.expires.Add(time.Minute),
		}); err != nil {
			return apitypes.CreditPurchase{}, fmt.Errorf("record checkout: %w", err)
		}
	}
	return b.CreditPurchase(ctx, user, purchase.ID)
}

// CreditPurchase returns one of the account's purchases.
func (b *Billing) CreditPurchase(ctx context.Context, user, id uuid.UUID) (apitypes.CreditPurchase, error) {
	p, err := b.queries.Purchase(ctx, id)
	if errors.Is(err, pgx.ErrNoRows) || (err == nil && p.UserID != user) {
		return apitypes.CreditPurchase{}, ErrNotFound
	}
	if err != nil {
		return apitypes.CreditPurchase{}, fmt.Errorf("read purchase: %w", err)
	}
	out := apitypes.CreditPurchase{
		Id: p.ID, Kind: apitypes.CreditPurchaseKind(p.Kind), AmountNanos: p.AmountNanos,
		Status: apitypes.CreditPaymentStatus(p.Status), FundedAt: p.FundedAt, ReversedNanos: p.ReversedNanos, CreatedAt: p.CreatedAt,
	}
	if p.Status == paymentPending {
		out.CheckoutUrl = p.CheckoutUrl
	}
	return out, nil
}

// settlePurchase reads what Stripe holds of a purchase and records it:
// funds it once on success, takes back refunds and disputes, and pauses
// automatic reload on a decline or a card that needs authentication. A
// purchase whose Stripe call never happened makes it now, under the same
// idempotency key.
func (b *Billing) settlePurchase(ctx context.Context, id uuid.UUID) error {
	s, err := b.payments()
	if err != nil {
		return err
	}
	row, err := b.queries.Purchase(ctx, id)
	if err != nil {
		return fmt.Errorf("read purchase %s: %w", id, err)
	}
	customer, err := b.queries.CustomerOf(ctx, row.UserID)
	if err != nil {
		return fmt.Errorf("read customer: %w", err)
	}
	if customer.StripeCustomerID == nil {
		return fmt.Errorf("purchase %s belongs to an account with no stripe customer", id)
	}
	var p payment
	switch {
	case row.PaymentIntentID != nil:
		p, err = s.payment(ctx, *row.PaymentIntentID)
	case row.CheckoutSessionID != nil:
		p, err = s.checkoutPayment(ctx, *row.CheckoutSessionID)
	case row.Status != paymentPending:
		return nil
	case row.Kind == purchaseAutomatic:
		// The payment may exist with its creation response lost: find it by
		// the purchase id it carries before charging again or giving up.
		found, ferr := s.paymentOfPurchase(ctx, row.ID)
		if ferr != nil {
			return ferr
		}
		switch {
		case found != "":
			if err := b.queries.PurchaseIntentFound(ctx, PurchaseIntentFoundParams{ID: row.ID, PaymentIntentID: found}); err != nil {
				return fmt.Errorf("record found payment: %w", err)
			}
			p, err = s.payment(ctx, found)
		case time.Since(row.CreatedAt) > purchaseRetryWindow:
			p = payment{purchase: id.String(), status: paymentCancelled}
		default:
			eligible, eerr := b.queries.ReloadEligible(ctx, row.UserID)
			if eerr != nil {
				return fmt.Errorf("check reload: %w", eerr)
			}
			if !eligible {
				p = payment{purchase: id.String(), status: paymentCancelled}
				break
			}
			p, err = s.chargeCard(ctx, *customer.StripeCustomerID, row.ID, row.AmountNanos/nanosPerCent)
		}
	case time.Since(row.CreatedAt) > purchaseRetryWindow:
		p = payment{purchase: id.String(), status: paymentCancelled}
	case row.Kind == purchaseManual:
		session, err := s.creditCheckout(ctx, *customer.StripeCustomerID, row.ID, row.AmountNanos/nanosPerCent, row.SuccessUrl, row.CancelUrl)
		if err != nil {
			return err
		}
		return b.queries.SetCheckout(ctx, SetCheckoutParams{
			ID: row.ID, CheckoutSessionID: &session.id, CheckoutUrl: &session.url, CheckoutExpiresAt: &session.expires,
			NextAttemptAt: session.expires.Add(time.Minute),
		})
	}
	if err != nil {
		return err
	}
	if p.purchase != id.String() || (p.customer != "" && p.customer != *customer.StripeCustomerID) ||
		(p.intent != "" && p.amount != 0 && p.amount*nanosPerCent != row.AmountNanos) {
		return fmt.Errorf("stripe payment %s does not match purchase %s", p.intent, id)
	}
	err = pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		locked, err := q.LockPurchase(ctx, id)
		if err != nil {
			return fmt.Errorf("lock purchase: %w", err)
		}
		return recordPayment(ctx, q, locked, p)
	})
	if err != nil {
		return fmt.Errorf("settle purchase %s: %w", id, err)
	}
	return nil
}

func recordPayment(ctx context.Context, q *Queries, row CreditPurchase, p payment) error {
	params := SettlePurchaseParams{ID: row.ID, Status: p.status, ReversedNanos: row.ReversedNanos, NextAttemptAt: time.Now().Add(time.Minute)}
	if p.intent != "" {
		params.PaymentIntentID = &p.intent
	}
	lotID := row.LotID
	if p.status == paymentSucceeded {
		if lotID == nil {
			now := time.Now()
			id, err := addCredit(ctx, q, creditGrant{
				user: row.UserID, kind: kindPurchased, source: "purchase:" + row.ID.String(), amount: row.AmountNanos, effective: now,
			})
			if err != nil {
				return err
			}
			lotID, params.LotID, params.FundedAt = &id, &id, &now
		}
		params.ReversedNanos = max(0, row.AmountNanos-p.retained*nanosPerCent)
		if params.ReversedNanos != row.ReversedNanos {
			if err := q.ReverseCredit(ctx, ReverseCreditParams{ID: *lotID, ReversedNanos: params.ReversedNanos}); err != nil {
				return fmt.Errorf("reverse credit: %w", err)
			}
		}
		params.NextAttemptAt = time.Now().Add(settledRecheck)
	}
	if err := q.SettlePurchase(ctx, params); err != nil {
		return fmt.Errorf("record payment: %w", err)
	}
	if row.Kind == purchaseAutomatic && (p.status == paymentDeclined || p.status == paymentActionRequired) {
		if err := q.PauseReload(ctx, PauseReloadParams{UserID: row.UserID, PurchaseID: &row.ID, Reason: &p.status}); err != nil {
			return fmt.Errorf("pause reload: %w", err)
		}
	}
	return nil
}

// SweepPurchases settles pending purchases Stripe's deliveries have not:
// calls that never returned, checkouts past their expiry and payments still
// processing. A purchase that fails is retried with backoff.
func (b *Billing) SweepPurchases(ctx context.Context) (int, error) {
	if b.stripe == nil {
		return 0, nil
	}
	due, err := b.queries.DuePurchases(ctx, paymentSweepBatch)
	if err != nil {
		return 0, fmt.Errorf("list due purchases: %w", err)
	}
	for _, id := range due {
		if err := b.settlePurchase(ctx, id); err != nil {
			b.logger.WarnContext(ctx, "settle credit purchase", "purchase_id", id, "error", err)
			if err := b.queries.RetryPurchase(ctx, RetryPurchaseParams{ID: id, LastError: truncate(err.Error()), NextAttemptAt: time.Now().Add(5 * time.Minute)}); err != nil {
				return 0, fmt.Errorf("retry purchase: %w", err)
			}
		}
	}
	return len(due), nil
}

// Reload charges the saved card of every account whose balance reached its
// reload threshold, one automatic payment per account at a time.
func (b *Billing) Reload(ctx context.Context) (int, error) {
	if b.stripe == nil {
		return 0, nil
	}
	due, err := b.queries.DueReloads(ctx, reloadBatch)
	if err != nil {
		return 0, fmt.Errorf("list due reloads: %w", err)
	}
	started := 0
	for _, account := range due {
		id, ok, err := b.startReload(ctx, account.UserID)
		if err != nil {
			return started, err
		}
		if !ok {
			continue
		}
		started++
		if err := b.settlePurchase(ctx, id); err != nil {
			b.logger.WarnContext(ctx, "automatic reload", "user_id", account.UserID, "purchase_id", id, "error", err)
		}
	}
	return started, nil
}

// startReload decides one account's reload under its balance lock: the
// balance is settled first, so credit a reload just added counts, and the
// purchase is inserted in the same transaction as the decision.
func (b *Billing) startReload(ctx context.Context, user uuid.UUID) (uuid.UUID, bool, error) {
	var id uuid.UUID
	started := false
	err := pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		account, err := q.LockBalance(ctx, user)
		if err != nil {
			return fmt.Errorf("lock balance: %w", err)
		}
		if err := settle(ctx, q, user, account.ComplimentarySince, account.Now); err != nil {
			return err
		}
		cents, err := q.ReloadAmount(ctx, user)
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("read reload: %w", err)
		}
		id, err = q.InsertAutomaticPurchase(ctx, InsertAutomaticPurchaseParams{UserID: user, AmountNanos: int64(cents) * nanosPerCent})
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("insert automatic purchase: %w", err)
		}
		started = true
		return nil
	})
	if err != nil {
		return uuid.UUID{}, false, fmt.Errorf("start automatic reload: %w", err)
	}
	return id, started, nil
}

func truncate(s string) string {
	const limit = 500
	if len(s) > limit {
		return s[:limit]
	}
	return s
}
