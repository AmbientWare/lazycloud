package billing

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// creditGrant is credit to add to an account, once per source.
type creditGrant struct {
	user      uuid.UUID
	kind      string
	source    string
	amount    int64
	effective time.Time
	expires   *time.Time
}

// addCredit writes g unless its source was already granted, and returns
// the lot either way. A new lot makes the account due in the caller's
// transaction, so the credit covers any negative balance first.
func addCredit(ctx context.Context, q *Queries, g creditGrant) (uuid.UUID, error) {
	id, err := q.GrantCredit(ctx, GrantCreditParams{
		UserID: g.user, Kind: g.kind, Source: g.source, AmountNanos: g.amount, EffectiveAt: g.effective, ExpiresAt: g.expires,
	})
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		id, err = q.LotBySource(ctx, LotBySourceParams{UserID: g.user, Source: g.source})
	case err == nil:
		err = q.MarkBalancesDue(ctx, []uuid.UUID{g.user})
	}
	if err != nil {
		return uuid.UUID{}, fmt.Errorf("grant %s credit %s: %w", g.kind, g.source, err)
	}
	return id, nil
}

// ensureAccount creates user's account with its trial credit when it does
// not exist yet.
func ensureAccount(ctx context.Context, q *Queries, user uuid.UUID) error {
	return ensureAccounts(ctx, q, []uuid.UUID{user})
}

// ensureAccounts creates the accounts of users that do not exist yet, each
// with its trial credit and a balance holding it. The three inserts run in
// the caller's transaction, so no account exists without its trial and
// balance; only accounts the first insert created get the other two.
func ensureAccounts(ctx context.Context, q *Queries, users []uuid.UUID) error {
	created, err := q.InsertAccounts(ctx, users)
	if err != nil {
		return fmt.Errorf("create billing accounts: %w", err)
	}
	if len(created) == 0 {
		return nil
	}
	ids := make([]uuid.UUID, len(created))
	at := make([]time.Time, len(created))
	for i, a := range created {
		ids[i], at[i] = a.UserID, a.CreatedAt
	}
	if err := q.InsertTrialCredit(ctx, InsertTrialCreditParams{TrialNanos: TrialNanos, TrialDays: TrialDays, UserIds: ids, CreatedAts: at}); err != nil {
		return fmt.Errorf("grant trial credit: %w", err)
	}
	if err := q.InsertTrialBalances(ctx, InsertTrialBalancesParams{TrialNanos: TrialNanos, TrialDays: TrialDays, UserIds: ids, CreatedAts: at}); err != nil {
		return fmt.Errorf("create billing balances: %w", err)
	}
	return nil
}
