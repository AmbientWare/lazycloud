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
// the lot either way.
func addCredit(ctx context.Context, q *Queries, g creditGrant) (uuid.UUID, error) {
	id, err := q.GrantCredit(ctx, GrantCreditParams{
		UserID: g.user, Kind: g.kind, Source: g.source, AmountNanos: g.amount, EffectiveAt: g.effective, ExpiresAt: g.expires,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		id, err = q.LotBySource(ctx, LotBySourceParams{UserID: g.user, Source: g.source})
	}
	if err != nil {
		return uuid.UUID{}, fmt.Errorf("grant %s credit %s: %w", g.kind, g.source, err)
	}
	return id, nil
}

// ensureAccount creates user's account with its trial credit when it does
// not exist yet.
func ensureAccount(ctx context.Context, q *Queries, user uuid.UUID) error {
	if err := q.EnsureAccounts(ctx, EnsureAccountsParams{UserIds: []uuid.UUID{user}, TrialNanos: TrialNanos, TrialDays: TrialDays}); err != nil {
		return fmt.Errorf("ensure billing account: %w", err)
	}
	return nil
}
