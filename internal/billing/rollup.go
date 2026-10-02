package billing

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// rollupBatch bounds the accounts one rollup pass settles; each settles in
// its own transaction.
const rollupBatch = 200

const (
	kindTrial        = "trial"
	kindSubscription = "subscription"
	kindPurchased    = "purchased"
)

// RollupResult counts one rollup pass.
type RollupResult struct {
	Accounts int
	// More means the pass stopped at its batch bound with accounts still
	// due.
	More bool
}

// Rollup settles due accounts: it covers their uncovered hours from credit,
// oldest hour first, and writes each balance and month's spend. An account
// fails alone and stays due.
func (b *Billing) Rollup(ctx context.Context) (RollupResult, error) {
	var result RollupResult
	for result.Accounts < rollupBatch {
		settled, err := b.rollupOne(ctx, nil)
		if err != nil {
			return result, err
		}
		if !settled {
			return result, nil
		}
		result.Accounts++
	}
	result.More = true
	return result, nil
}

// rollupOne settles the account user, or claims the next due account when
// user is nil. It reports whether it settled one.
func (b *Billing) rollupOne(ctx context.Context, user *uuid.UUID) (bool, error) {
	settled := false
	err := pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		var account ClaimRollupRow
		if user == nil {
			row, err := q.ClaimRollup(ctx)
			if errors.Is(err, pgx.ErrNoRows) {
				return nil
			}
			if err != nil {
				return fmt.Errorf("claim due balance: %w", err)
			}
			account = row
		} else {
			row, err := q.LockBalance(ctx, *user)
			if err != nil {
				return fmt.Errorf("lock balance: %w", err)
			}
			account = ClaimRollupRow(row)
		}
		if err := settle(ctx, q, account.UserID, account.ComplimentarySince, account.Now); err != nil {
			return fmt.Errorf("settle account %s: %w", account.UserID, err)
		}
		settled = true
		return nil
	})
	if err != nil {
		return false, fmt.Errorf("roll up balance: %w", err)
	}
	return settled, nil
}

type lot struct {
	id        uuid.UUID
	kind      string
	remaining int64
	effective time.Time
	expires   *time.Time
	spent     int64
}

// usable reports whether the lot may cover usage at hour, given it is
// spent at now: it exists by now and had not expired when the usage
// happened. Credit granted after an hour still pays it, which is how new
// credit covers a negative balance first.
func (l *lot) usable(hour, now time.Time) bool {
	return l.remaining > 0 && !l.effective.After(now) && (l.expires == nil || l.expires.After(hour))
}

func (l *lot) live(now time.Time) bool {
	return !l.effective.After(now) && (l.expires == nil || l.expires.After(now))
}

type hour struct {
	at                                   time.Time
	uncovered                            int64
	coveredCredit, coveredSub, coveredWv int64
}

// settle runs under the account's balance lock. It reads uncovered hours and
// open lots, covers the hours, settles lots a reversal overdrew from other
// credit, and writes the balance.
func settle(ctx context.Context, q *Queries, user uuid.UUID, complimentarySince *time.Time, now time.Time) error {
	hourRows, err := q.UncoveredHours(ctx, user)
	if err != nil {
		return fmt.Errorf("read uncovered hours: %w", err)
	}
	lotRows, err := q.OpenLots(ctx, user)
	if err != nil {
		return fmt.Errorf("read lots: %w", err)
	}
	lots := make([]*lot, len(lotRows))
	for n, r := range lotRows {
		lots[n] = &lot{id: r.ID, kind: r.Kind, remaining: r.RemainingNanos, effective: r.EffectiveAt, expires: r.ExpiresAt}
	}
	hours := make([]*hour, len(hourRows))
	for n, r := range hourRows {
		hours[n] = &hour{at: r.Hour, uncovered: r.CostNanos - r.CreditedNanos - r.WaivedNanos}
	}

	// A lot a refund or dispute overdrew is debt; other live credit pays it.
	for _, debtor := range lots {
		for _, payer := range lots {
			if debtor.remaining >= 0 {
				break
			}
			if payer == debtor || payer.remaining <= 0 || !payer.live(now) {
				continue
			}
			take := min(-debtor.remaining, payer.remaining)
			payer.remaining, payer.spent = payer.remaining-take, payer.spent+take
			debtor.remaining, debtor.spent = debtor.remaining+take, debtor.spent-take
		}
	}
	for _, h := range hours {
		if complimentarySince != nil && !h.at.Add(time.Hour).Before(*complimentarySince) {
			// Usage while complimentary keeps its price and costs nothing.
			h.coveredWv, h.uncovered = h.uncovered, 0
			continue
		}
		for _, l := range lots {
			if h.uncovered == 0 {
				break
			}
			if !l.usable(h.at, now) {
				continue
			}
			take := min(h.uncovered, l.remaining)
			l.remaining, l.spent = l.remaining-take, l.spent+take
			h.uncovered -= take
			h.coveredCredit += take
			if l.kind == kindSubscription {
				h.coveredSub += take
			}
		}
	}

	var spend SpendLotsParams
	for _, l := range lots {
		if l.spent != 0 {
			spend.Ids, spend.Deltas = append(spend.Ids, l.id), append(spend.Deltas, l.spent)
		}
	}
	if len(spend.Ids) > 0 {
		if err := q.SpendLots(ctx, spend); err != nil {
			return fmt.Errorf("spend lots: %w", err)
		}
	}
	cover := CoverHoursParams{UserID: user}
	var debt int64
	for _, h := range hours {
		debt += h.uncovered
		if h.coveredCredit == 0 && h.coveredWv == 0 {
			continue
		}
		cover.Hours = append(cover.Hours, h.at)
		cover.Credited = append(cover.Credited, h.coveredCredit)
		cover.Subscription = append(cover.Subscription, h.coveredSub)
		cover.Waived = append(cover.Waived, h.coveredWv)
	}
	if len(cover.Hours) > 0 {
		if err := q.CoverHours(ctx, cover); err != nil {
			return fmt.Errorf("cover hours: %w", err)
		}
	}

	month := monthStart(now)
	recheck := month.AddDate(0, 1, 0)
	balance := -debt
	for _, l := range lots {
		switch {
		case l.remaining < 0:
			balance += l.remaining
		case l.remaining > 0 && l.live(now):
			balance += l.remaining
			if l.expires != nil && l.expires.Before(recheck) {
				recheck = *l.expires
			}
		case l.remaining > 0 && l.effective.After(now) && l.effective.Before(recheck):
			recheck = l.effective
		}
	}
	spent, err := q.MonthSpent(ctx, MonthSpentParams{UserID: user, MonthStartedAt: month, MonthEndedAt: month.AddDate(0, 1, 0)})
	if err != nil {
		return fmt.Errorf("read month spend: %w", err)
	}
	if err := q.SetBalance(ctx, SetBalanceParams{
		UserID: user, BalanceNanos: balance, MonthStartedAt: month, MonthSpentNanos: spent, RecheckAt: recheck,
	}); err != nil {
		return fmt.Errorf("write balance: %w", err)
	}
	return nil
}

// monthStart is the first instant of t's UTC month.
func monthStart(t time.Time) time.Time {
	t = t.UTC()
	return time.Date(t.Year(), t.Month(), 1, 0, 0, 0, 0, time.UTC)
}
