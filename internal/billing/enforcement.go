package billing

import (
	"context"
	"fmt"

	"github.com/google/uuid"
)

// Unfunded is an account running containers it may not run, and why.
type Unfunded struct {
	User       uuid.UUID
	Workspaces []uuid.UUID
	// Reason is said to the containers' tasks as they stop.
	Reason string
}

// UnfundedAccounts are the accounts with live containers that have no
// credit left, reached their monthly usage limit or have a payment past
// due. Their containers must stop: running compute shuts down, and
// admission already refuses new work.
func (b *Billing) UnfundedAccounts(ctx context.Context) ([]Unfunded, error) {
	rows, err := b.queries.UnfundedAccounts(ctx)
	if err != nil {
		return nil, fmt.Errorf("list unfunded accounts: %w", err)
	}
	out := make([]Unfunded, 0, len(rows))
	for _, r := range rows {
		reason := "the account's credit ran out"
		switch {
		case r.PastDue:
			reason = "a payment for the account did not go through"
		case r.OverLimit && !r.NoCredit:
			reason = "the account reached its monthly usage limit"
		}
		workspaces, err := b.queries.OwnedWorkspaces(ctx, r.UserID)
		if err != nil {
			return nil, fmt.Errorf("list owned workspaces: %w", err)
		}
		out = append(out, Unfunded{User: r.UserID, Workspaces: workspaces, Reason: reason})
	}
	return out, nil
}
