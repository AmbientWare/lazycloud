package billing

import (
	"context"
	"fmt"
	"html"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/notifications"
)

const (
	// UnfundedRetention is how long an account without credit keeps its
	// stored data, free of storage charges.
	UnfundedRetention   = 30 * 24 * time.Hour
	unfundedBatch       = 100
	unfundedSubject     = "Add credit within 30 days to keep your stored data"
	unfundedRetainDays  = 30
	unfundedDeadlineFmt = "2006-01-02"
)

// RetentionResult counts one retention pass.
type RetentionResult struct {
	Started int
	Ended   int
}

// SweepRetention starts the retention period of every account that ran out
// of credit while storing data, with the warning email in the same
// transaction, and ends the periods of accounts whose credit returned,
// withdrawing an unsent warning.
func (b *Billing) SweepRetention(ctx context.Context) (RetentionResult, error) {
	var out RetentionResult
	err := pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		ended, err := q.EndFundedPeriods(ctx)
		if err != nil {
			return fmt.Errorf("end funded periods: %w", err)
		}
		var unsent []notifications.MessageID
		for _, id := range ended {
			if id != nil {
				unsent = append(unsent, notifications.MessageID(*id))
			}
		}
		if err := notifications.Discard(ctx, tx, unsent); err != nil {
			return err
		}
		out.Ended = len(ended)
		started, err := q.StartUnfundedPeriods(ctx, unfundedBatch)
		if err != nil {
			return fmt.Errorf("start unfunded periods: %w", err)
		}
		for _, p := range started {
			if err := warnUnfunded(ctx, tx, q, p.UserID, p.StartedAt); err != nil {
				return err
			}
		}
		out.Started = len(started)
		return nil
	})
	if err != nil {
		return RetentionResult{}, fmt.Errorf("sweep unfunded retention: %w", err)
	}
	return out, nil
}

// warnUnfunded queues the warning that the account's data will be deleted
// unless credit returns.
func warnUnfunded(ctx context.Context, tx pgx.Tx, q *Queries, user uuid.UUID, started time.Time) error {
	email, err := q.OwnerEmail(ctx, user)
	if err != nil {
		return fmt.Errorf("read account email: %w", err)
	}
	if email == nil || *email == "" {
		return nil
	}
	deadline := started.Add(UnfundedRetention).UTC().Format(unfundedDeadlineFmt)
	body := "Your credit balance is empty. Your stored files will be retained at no charge until " + deadline +
		". Restore a positive credit balance before that date to keep your data. Otherwise, platform-managed files," +
		" volumes and disks will be permanently deleted. Workspaces in your connected AWS account are unaffected."
	id, err := notifications.Enqueue(ctx, tx, notifications.Email{
		To: *email, Subject: unfundedSubject, Text: body, HTML: "<p>" + html.EscapeString(body) + "</p>",
	})
	if err != nil {
		return err
	}
	message := uuid.UUID(id)
	if err := q.SetUnfundedMessage(ctx, SetUnfundedMessageParams{UserID: user, MessageID: &message}); err != nil {
		return fmt.Errorf("record warning: %w", err)
	}
	return nil
}

// ExpiredUnfunded returns the accounts that stayed without credit through
// the retention period, with the workspaces whose data storage deletes.
func (b *Billing) ExpiredUnfunded(ctx context.Context) ([]Unfunded, error) {
	// Settle each expired account first, so credit added on the last day
	// counts before anything is deleted.
	users, err := b.queries.ExpiredUnfundedAccounts(ctx, unfundedRetainDays)
	if err != nil {
		return nil, fmt.Errorf("list expired retention: %w", err)
	}
	for _, user := range users {
		if _, err := b.rollupOne(ctx, &user); err != nil {
			return nil, err
		}
	}
	rows, err := b.queries.ExpiredUnfundedWorkspaces(ctx, unfundedRetainDays)
	if err != nil {
		return nil, fmt.Errorf("list expired retention: %w", err)
	}
	var out []Unfunded
	for _, r := range rows {
		if len(out) == 0 || out[len(out)-1].User != r.UserID {
			out = append(out, Unfunded{User: r.UserID, Reason: "the account stayed without credit for 30 days"})
		}
		last := &out[len(out)-1]
		last.Workspaces = append(last.Workspaces, r.WorkspaceID)
	}
	return out, nil
}

// EndRetention ends an expired period once storage holds none of the
// account's data, so abandoned accounts are not read again. Data stored
// later starts a new period.
func (b *Billing) EndRetention(ctx context.Context, user uuid.UUID) error {
	if err := b.queries.EndUnfundedPeriod(ctx, user); err != nil {
		return fmt.Errorf("end retention: %w", err)
	}
	return nil
}
