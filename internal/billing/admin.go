package billing

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// recentCostWindow is how far back the spend beside each account reaches:
// a fixed trailing window, because each account's billing period opens on
// a different day.
const recentCostWindow = 30 * 24 * time.Hour

// AccountFilter narrows the administrator's account list.
type AccountFilter struct {
	Search string
	// Admin selects administrators (true) or other users (false).
	Admin  *bool
	Status *apitypes.UserStatus
	Cursor string
	Limit  int
}

// Accounts lists every user with their billing standing and recent spend,
// for platform administrators. The caller authorizes.
func (b *Billing) Accounts(ctx context.Context, f AccountFilter) (apitypes.BillingAccountAdminPage, error) {
	after := uuid.Nil
	if f.Cursor != "" {
		raw, err := base64.RawURLEncoding.DecodeString(f.Cursor)
		if err != nil || len(raw) != len(after) {
			return apitypes.BillingAccountAdminPage{}, &InvalidError{Message: "the cursor is not from a previous page"}
		}
		copy(after[:], raw)
	}
	pattern := ""
	if f.Search != "" {
		pattern = "%" + strings.NewReplacer(`\`, `\\`, "%", `\%`, "_", `\_`).Replace(f.Search) + "%"
	}
	since := time.Now().Add(-recentCostWindow).UTC().Truncate(time.Hour)
	params := AdminAccountsParams{Since: since, After: after, Pattern: pattern, IsAdmin: f.Admin, RowLimit: int32(f.Limit + 1)} //nolint:gosec // The API bounds the limit to 200.
	if f.Status != nil {
		status := string(*f.Status)
		params.UserStatus = &status
	}
	rows, err := b.queries.AdminAccounts(ctx, params)
	if err != nil {
		return apitypes.BillingAccountAdminPage{}, fmt.Errorf("list billing accounts: %w", err)
	}
	out := apitypes.BillingAccountAdminPage{Accounts: []apitypes.BillingAccountAdmin{}}
	for n, row := range rows {
		if n == f.Limit {
			next := base64.RawURLEncoding.EncodeToString(rows[n-1].ID[:])
			out.NextCursor = &next
			break
		}
		account, err := adminAccountOut(AdminAccountRow(row), since)
		if err != nil {
			return apitypes.BillingAccountAdminPage{}, err
		}
		out.Accounts = append(out.Accounts, account)
	}
	return out, nil
}

// SetComplimentary waives the user's usage charges from now, or stops
// waiving them. Usage keeps its price either way. The caller authorizes.
func (b *Billing) SetComplimentary(ctx context.Context, user uuid.UUID, complimentary bool) (apitypes.BillingAccountAdmin, error) {
	since := time.Now().Add(-recentCostWindow).UTC().Truncate(time.Hour)
	var row AdminAccountRow
	err := pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		if _, err := q.AccountUser(ctx, user); errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		} else if err != nil {
			return fmt.Errorf("read user: %w", err)
		}
		if err := ensureAccount(ctx, q, user); err != nil {
			return err
		}
		if _, err := q.SetComplimentary(ctx, SetComplimentaryParams{UserID: user, Complimentary: complimentary}); err != nil {
			return fmt.Errorf("set complimentary: %w", err)
		}
		var err error
		row, err = q.AdminAccount(ctx, AdminAccountParams{ID: user, Since: since})
		return err
	})
	if err != nil {
		return apitypes.BillingAccountAdmin{}, fmt.Errorf("set complimentary: %w", err)
	}
	return adminAccountOut(row, since)
}

// SetComplimentaryByEmail is SetComplimentary for the user with email. The
// local stack waives its development account, which has no way to pay.
func (b *Billing) SetComplimentaryByEmail(ctx context.Context, email string, complimentary bool) error {
	user, err := b.queries.UserByEmail(ctx, &email)
	if errors.Is(err, pgx.ErrNoRows) {
		return fmt.Errorf("user %s: %w", email, ErrNotFound)
	}
	if err != nil {
		return fmt.Errorf("read user: %w", err)
	}
	_, err = b.SetComplimentary(ctx, user, complimentary)
	return err
}

func adminAccountOut(row AdminAccountRow, since time.Time) (apitypes.BillingAccountAdmin, error) {
	out := apitypes.BillingAccountAdmin{
		User: apitypes.User{
			Id: row.ID, DisplayName: row.DisplayName, AvatarUrl: row.AvatarUrl, GithubLogin: row.GithubLogin,
			IsAdmin: row.IsAdmin, Status: apitypes.UserStatus(row.UserStatus), CreatedAt: row.CreatedAt,
		},
		PaymentMethodOnFile: row.PaymentMethodAttachedAt != nil,
		ComplimentarySince:  row.ComplimentarySince,
		RecentCostNanos:     row.RecentCostNanos,
		RecentCostSince:     since,
	}
	if row.Email != nil {
		out.User.Email = *row.Email
	}
	if row.TermsVersion != nil {
		plan, err := planOfTerms(TermsVersion(*row.TermsVersion))
		if err != nil {
			return apitypes.BillingAccountAdmin{}, err
		}
		id := apitypes.PlanId(plan.ID)
		out.Plan = &id
	}
	if row.Status != nil {
		status := apitypes.BillingStatus(*row.Status)
		out.Status = &status
	}
	return out, nil
}
