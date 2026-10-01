package billing

import (
	"context"
	"errors"
	"fmt"
	"math/big"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/stripe/stripe-go/v87"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

const (
	changeOpen    = "open"
	changeApplied = "applied"
	changeFailed  = "failed"
	// planChangeWindow is how long a change whose outcome Stripe never
	// confirmed is retried under its idempotency keys.
	planChangeWindow = 20 * time.Hour
	planChangeBatch  = 20
	statusActive     = "active"
)

type planChange struct {
	id        uuid.UUID
	user      uuid.UUID
	to        TermsVersion
	attempts  int32
	createdAt time.Time
}

// ChangePlan moves the account onto the published terms req names. A
// dearer plan applies now and charges the prorated difference; a cheaper
// one, Free included, applies at renewal; the current terms cancel a
// scheduled change. The change row is committed before Stripe is called and
// keys every call, and an answer that never arrives leaves it open for the
// settler.
func (b *Billing) ChangePlan(ctx context.Context, user uuid.UUID, req apitypes.PlanChangeRequest) (apitypes.BillingAccount, error) {
	if _, err := b.payments(); err != nil {
		return apitypes.BillingAccount{}, err
	}
	target, err := planOfTerms(TermsVersion(req.TermsVersion))
	if err != nil || target.ID != PlanID(req.Plan) {
		return apitypes.BillingAccount{}, &InvalidError{Message: "these terms are not the published terms of that plan"}
	}
	var change *planChange
	err = pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		if err := ensureAccount(ctx, q, user); err != nil {
			return err
		}
		account, err := q.LockAccount(ctx, user)
		if err != nil {
			return fmt.Errorf("lock account: %w", err)
		}
		if account.ComplimentarySince != nil {
			return &ConflictError{Message: "this account's usage is complimentary; it holds no plan to change"}
		}
		current, err := planOfTerms(TermsVersion(account.TermsVersion))
		if err != nil {
			return err
		}
		if current.Terms == target.Terms && account.ScheduledTermsVersion == nil {
			return nil
		}
		if current.Terms != target.Terms {
			if target.MonthlyNanos > current.MonthlyNanos && account.PaymentMethodAttachedAt == nil {
				return &PaymentRequiredError{Message: "add a payment method before upgrading your subscription"}
			}
			if err := planFits(ctx, q, user, target, account.PaymentMethodAttachedAt != nil); err != nil {
				return err
			}
		}
		row, err := q.InsertPlanChange(ctx, InsertPlanChangeParams{UserID: user, FromTerms: account.TermsVersion, ToTerms: string(target.Terms)})
		if isUniqueViolation(err) {
			return &ConflictError{Message: "a plan change for this account is already being settled"}
		}
		if err != nil {
			return fmt.Errorf("insert plan change: %w", err)
		}
		change = &planChange{id: row.ID, user: user, to: target.Terms, attempts: row.Attempts, createdAt: row.CreatedAt}
		return nil
	})
	if err != nil {
		return apitypes.BillingAccount{}, fmt.Errorf("change plan: %w", err)
	}
	if change != nil {
		if err := b.settlePlanChange(ctx, *change); err != nil {
			return apitypes.BillingAccount{}, err
		}
	}
	return b.Account(ctx, user)
}

// planFits refuses a change onto terms whose limits the account already
// exceeds, naming what has to go first.
func planFits(ctx context.Context, q *Queries, user uuid.UUID, target Plan, hasCard bool) error {
	e, err := accountEntitlements(target, hasCard, false)
	if err != nil {
		return err
	}
	var violations []string
	workspaces, err := q.OwnedWorkspaceCount(ctx, user)
	if err != nil {
		return fmt.Errorf("count workspaces: %w", err)
	}
	if !e.MaxWorkspaces.Unlimited && int(workspaces) > e.MaxWorkspaces.Max {
		violations = append(violations, fmt.Sprintf("%d workspaces (limit %d)", workspaces, e.MaxWorkspaces.Max))
	}
	members, err := q.OwnerMemberCount(ctx, user)
	if err != nil {
		return fmt.Errorf("count members: %w", err)
	}
	if !e.MaxMembers.Unlimited && int(members) > e.MaxMembers.Max {
		violations = append(violations, fmt.Sprintf("%d members (limit %d)", members, e.MaxMembers.Max))
	}
	if len(violations) > 0 {
		return &ConflictError{Message: "this account cannot move to the requested plan while it has " + strings.Join(violations, ", ")}
	}
	return nil
}

// settlePlanChange makes the Stripe calls of one claimed change and records
// the outcome: applied with the subscription Stripe now holds, failed when
// Stripe refused it, or left open to retry when nobody knows.
func (b *Billing) settlePlanChange(ctx context.Context, change planChange) error {
	sub, err := b.applyPlanChange(ctx, change)
	if se, ok := refusal(err); ok {
		message := se.Msg
		if message == "" {
			message = "Stripe refused the change"
		}
		if ferr := b.queries.FinishPlanChange(ctx, FinishPlanChangeParams{ID: change.id, State: changeFailed, Error: truncate(message)}); ferr != nil {
			return fmt.Errorf("record refused plan change: %w", ferr)
		}
		if se.Type == stripe.ErrorTypeCard {
			return &PaymentRequiredError{Message: "your payment method was declined: " + message}
		}
		return &ConflictError{Message: "the plan change was refused: " + message}
	}
	if err != nil {
		if time.Since(change.createdAt) > planChangeWindow {
			if ferr := b.queries.FinishPlanChange(ctx, FinishPlanChangeParams{ID: change.id, State: changeFailed, Error: truncate(err.Error())}); ferr != nil {
				return fmt.Errorf("abandon plan change: %w", ferr)
			}
			b.logger.ErrorContext(ctx, "plan change abandoned", "plan_change_id", change.id, "user_id", change.user, "error", err)
			return nil
		}
		b.logger.WarnContext(ctx, "plan change undecided", "plan_change_id", change.id, "attempt", change.attempts, "error", err)
		next := time.Now().Add(min(30*time.Second<<min(change.attempts, 6), 30*time.Minute))
		if rerr := b.queries.RetryPlanChange(ctx, RetryPlanChangeParams{ID: change.id, NextAttemptAt: next, Error: truncate(err.Error())}); rerr != nil {
			return fmt.Errorf("defer plan change: %w", rerr)
		}
		return nil
	}
	err = pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		if sub != nil {
			if err := applySubscription(ctx, q, change.user, *sub); err != nil {
				return err
			}
		}
		return q.FinishPlanChange(ctx, FinishPlanChangeParams{ID: change.id, State: changeApplied})
	})
	if err != nil {
		return fmt.Errorf("record plan change: %w", err)
	}
	if sub != nil && sub.invoice != "" {
		if err := b.creditInvoice(ctx, sub.invoice); err != nil {
			b.logger.WarnContext(ctx, "subscription credit waits for Stripe's delivery", "invoice", sub.invoice, "error", err)
		}
	}
	return nil
}

// applyPlanChange decides the calls from what Stripe holds now, so a retry
// after a crash repeats only what is missing, under the same keys.
func (b *Billing) applyPlanChange(ctx context.Context, change planChange) (*subscription, error) {
	s, err := b.payments()
	if err != nil {
		return nil, err
	}
	ids, err := b.queries.StripeIdentity(ctx, change.user)
	if err != nil {
		return nil, fmt.Errorf("read stripe identity: %w", err)
	}
	key := "plan-change-" + change.id.String()
	target, err := planOfTerms(change.to)
	if err != nil {
		return nil, err
	}
	if ids.StripeSubscriptionID == nil {
		if change.to == TermsFree {
			return nil, nil
		}
		if ids.StripeCustomerID == nil {
			return nil, &ConflictError{Message: "the account has no payment method"}
		}
		sub, err := s.subscribe(ctx, *ids.StripeCustomerID, change.to, key)
		return &sub, err
	}
	current, err := s.subscription(ctx, *ids.StripeSubscriptionID)
	if err != nil {
		return nil, err
	}
	held, err := planOfTerms(current.terms)
	if err != nil {
		return nil, err
	}
	var sub subscription
	switch {
	case current.terms == change.to:
		sub, err = s.keep(ctx, current, key)
	case target.MonthlyNanos > held.MonthlyNanos:
		sub, err = s.upgrade(ctx, current, change.to, key)
	default:
		sub, err = s.scheduleDowngrade(ctx, current, change.to, key)
	}
	return &sub, err
}

// SettlePlanChanges retries open plan changes whose request died or whose
// outcome was unknown.
func (b *Billing) SettlePlanChanges(ctx context.Context) (int, error) {
	if b.stripe == nil {
		return 0, nil
	}
	rows, err := b.queries.ClaimPlanChanges(ctx, planChangeBatch)
	if err != nil {
		return 0, fmt.Errorf("claim plan changes: %w", err)
	}
	for _, row := range rows {
		change := planChange{id: row.ID, user: row.UserID, to: TermsVersion(row.ToTerms), attempts: row.Attempts, createdAt: row.CreatedAt}
		if err := b.settlePlanChange(ctx, change); err != nil {
			var refused *PaymentRequiredError
			var conflict *ConflictError
			if !errors.As(err, &refused) && !errors.As(err, &conflict) {
				return 0, err
			}
		}
	}
	return len(rows), nil
}

// applySubscription records the state a Stripe subscription implies, under
// the account lock. A delivery about a subscription the account no longer
// holds changes nothing.
func applySubscription(ctx context.Context, q *Queries, user uuid.UUID, sub subscription) error {
	held, err := q.LockSubscriber(ctx, user)
	if err != nil {
		return fmt.Errorf("lock subscriber: %w", err)
	}
	if held != nil && *held != sub.id {
		return nil
	}
	params := SetSubscriptionParams{UserID: user, TermsVersion: string(TermsFree), Status: statusActive}
	switch sub.status {
	case stripe.SubscriptionStatusCanceled, stripe.SubscriptionStatusIncompleteExpired:
		// Ended: the account is on Free, which has nothing to be past due
		// on.
	case stripe.SubscriptionStatusActive, stripe.SubscriptionStatusTrialing,
		stripe.SubscriptionStatusPastDue, stripe.SubscriptionStatusUnpaid,
		stripe.SubscriptionStatusIncomplete, stripe.SubscriptionStatusPaused:
		params.TermsVersion, params.SubscriptionID = string(sub.terms), &sub.id
		params.PeriodStartedAt, params.PeriodEndedAt = &sub.periodStart, &sub.periodEnd
		if sub.scheduled != nil {
			v := string(*sub.scheduled)
			params.ScheduledTermsVersion, params.ScheduledChangeAt = &v, sub.scheduledAt
		}
		if sub.status != stripe.SubscriptionStatusActive && sub.status != stripe.SubscriptionStatusTrialing {
			params.Status = statusPastDue
		}
	default:
		return fmt.Errorf("subscription %s has status %q", sub.id, sub.status)
	}
	if err := q.SetSubscription(ctx, params); err != nil {
		return fmt.Errorf("record subscription: %w", err)
	}
	return nil
}

// syncSubscription reads a subscription from Stripe and records it on the
// account its customer belongs to.
func (b *Billing) syncSubscription(ctx context.Context, id string) error {
	s, err := b.payments()
	if err != nil {
		return err
	}
	sub, err := s.subscription(ctx, id)
	if err != nil {
		return err
	}
	user, err := b.queries.AccountByCustomer(ctx, sub.customer)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("find account of %s: %w", sub.customer, err)
	}
	err = pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		return applySubscription(ctx, b.queries.WithTx(tx), user, sub)
	})
	if err != nil {
		return fmt.Errorf("sync subscription %s: %w", id, err)
	}
	return nil
}

// creditInvoice grants the subscription credit a paid invoice bought for
// the current period: a plan's included credit for a new or renewed
// period, and for an upgrade the difference over the time left, once per
// invoice.
func (b *Billing) creditInvoice(ctx context.Context, id string) error {
	s, err := b.payments()
	if err != nil {
		return err
	}
	inv, err := s.invoice(ctx, id)
	if err != nil {
		return err
	}
	if !inv.paid || inv.subscription == "" {
		return nil
	}
	user, err := b.queries.AccountByCustomer(ctx, inv.customer)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("find account of %s: %w", inv.customer, err)
	}
	sub, err := s.subscription(ctx, inv.subscription)
	if err != nil {
		return err
	}
	amount, effective, ok := includedCredit(inv, sub.periodStart, sub.periodEnd)
	if !ok {
		return nil
	}
	expires := sub.periodEnd
	err = pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		_, err := addCredit(ctx, b.queries.WithTx(tx), creditGrant{
			user: user, kind: kindSubscription, source: "invoice:" + inv.id, amount: amount, effective: effective, expires: &expires,
		})
		return err
	})
	if err != nil {
		return fmt.Errorf("grant subscription credit: %w", err)
	}
	return nil
}

// includedCredit is the usage credit an invoice's lines for the period
// [start, end) bought. A full-period line grants the plan's included
// credit; a prorated line grants its share of the period, capped by its
// share of the price; an unused-time line takes back the old plan's share.
func includedCredit(inv invoice, start, end time.Time) (int64, time.Time, bool) {
	duration := end.Sub(start).Microseconds()
	if duration <= 0 {
		return 0, time.Time{}, false
	}
	included := new(big.Rat)
	var effective time.Time
	positive := false
	for _, line := range inv.lines {
		if !line.periodEnd.Equal(end) || line.periodStart.Before(start) {
			continue
		}
		plan, err := planOfTerms(line.terms)
		if err != nil || plan.MonthlyNanos == 0 {
			continue
		}
		share := big.NewRat(line.periodEnd.Sub(line.periodStart).Microseconds(), duration)
		switch {
		case line.amount > 0:
			if line.proration {
				if paid := big.NewRat(line.amount*nanosPerCent, plan.MonthlyNanos); paid.Cmp(share) < 0 {
					share = paid
				}
			}
			included.Add(included, new(big.Rat).Mul(share, new(big.Rat).SetInt64(plan.IncludedNanos)))
			at := line.periodStart
			if line.proration && inv.paidAt.After(at) {
				at = inv.paidAt
			}
			if !positive || at.After(effective) {
				effective = at
			}
			positive = true
		case line.amount < 0:
			included.Sub(included, new(big.Rat).Mul(share, new(big.Rat).SetInt64(plan.IncludedNanos)))
		}
	}
	if !positive || included.Sign() <= 0 || !effective.Before(end) {
		return 0, time.Time{}, false
	}
	amount := new(big.Int).Quo(included.Num(), included.Denom())
	if amount.Sign() <= 0 {
		return 0, time.Time{}, false
	}
	return amount.Int64(), effective, true
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}
