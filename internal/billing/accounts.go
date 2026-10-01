package billing

import (
	"context"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// Account is the user's billing account as the dashboard shows it: plan
// and payment standing, effective limits and what the account holds of
// them, credit, the month's usage against its limit, and automatic reload.
func (b *Billing) Account(ctx context.Context, user uuid.UUID) (apitypes.BillingAccount, error) {
	var out apitypes.BillingAccount
	err := pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		if err := ensureAccount(ctx, q, user); err != nil {
			return err
		}
		var err error
		out, err = viewAccount(ctx, q, user)
		return err
	})
	if err != nil {
		return apitypes.BillingAccount{}, fmt.Errorf("read billing account: %w", err)
	}
	return out, nil
}

func viewAccount(ctx context.Context, q *Queries, user uuid.UUID) (apitypes.BillingAccount, error) {
	row, err := q.AccountView(ctx, user)
	if err != nil {
		return apitypes.BillingAccount{}, fmt.Errorf("read account: %w", err)
	}
	plan, err := planOfTerms(TermsVersion(row.TermsVersion))
	if err != nil {
		return apitypes.BillingAccount{}, err
	}
	hasCard, complimentary := row.PaymentMethodAttachedAt != nil, row.ComplimentarySince != nil
	entitlements, err := accountEntitlements(plan, hasCard, complimentary)
	if err != nil {
		return apitypes.BillingAccount{}, err
	}
	usage, err := entitlementUsage(ctx, q, user)
	if err != nil {
		return apitypes.BillingAccount{}, err
	}
	month := monthStart(row.Now)
	spent := row.AccruedNanos
	if row.MonthStartedAt.Equal(month) {
		spent += row.MonthSpentNanos
	}
	budget := apitypes.UsageBudget{MonthStartedAt: month, MonthEndedAt: month.AddDate(0, 1, 0), SpentNanos: spent, LimitNanos: row.MonthlyUsageLimitNanos}
	if row.MonthlyUsageLimitNanos != nil {
		available := max(0, *row.MonthlyUsageLimitNanos-spent)
		budget.AvailableNanos = &available
	}
	out := apitypes.BillingAccount{
		Status:   apitypes.BillingStatus(row.Status),
		Currency: Currency,
		Plan: apitypes.BillingPlan{
			Id: apitypes.PlanId(plan.ID), Name: plan.Name, TermsVersion: apitypes.TermsVersion(plan.Terms),
			MonthlyNanos: plan.MonthlyNanos, IncludedNanos: plan.IncludedNanos,
			ScheduledChangeAt: row.ScheduledChangeAt, PeriodStartedAt: row.PeriodStartedAt, PeriodEndedAt: row.PeriodEndedAt,
		},
		PortalAvailable:     row.StripeCustomerID != nil,
		PaymentMethodOnFile: hasCard,
		ComplimentarySince:  row.ComplimentarySince,
		Entitlements:        entitlementsOut(entitlements),
		Usage:               usage,
		BalanceNanos:        row.BalanceNanos - row.AccruedNanos,
		UsageBudget:         budget,
		Preferences: apitypes.BillingPreferences{
			MonthlyUsageLimitNanos: row.MonthlyUsageLimitNanos, ReloadEnabled: row.ReloadEnabled,
			ReloadThresholdCents: int(row.ReloadThresholdCents), ReloadAmountCents: int(row.ReloadAmountCents),
		},
		AutomaticReload: apitypes.AutomaticReload{
			MonthStartedAt: month, MonthEndedAt: month.AddDate(0, 1, 0),
			MonthlyPaymentCommittedCents: row.MonthAutomaticNanos / nanosPerCent,
			PausedPurchaseId:             uuidOut(row.ReloadPausedPurchaseID),
			PendingPurchaseId:            uuidOut(row.PendingPurchaseID),
		},
		PlanChangePending: row.PlanChangePending,
	}
	if row.ScheduledTermsVersion != nil {
		v := apitypes.TermsVersion(*row.ScheduledTermsVersion)
		out.Plan.ScheduledTermsVersion = &v
	}
	if row.ReloadPauseReason != nil {
		reason := apitypes.ReloadPauseReason(*row.ReloadPauseReason)
		out.AutomaticReload.PauseReason = &reason
	}
	return out, nil
}

func uuidOut(id *uuid.UUID) *openapi_types.UUID {
	if id == nil {
		return nil
	}
	out := *id
	return &out
}

// entitlementUsage is what the account holds of each limit across every
// workspace it owns.
func entitlementUsage(ctx context.Context, q *Queries, user uuid.UUID) (apitypes.EntitlementUsage, error) {
	live, err := q.OwnerLiveContainers(ctx, user)
	if err != nil {
		return apitypes.EntitlementUsage{}, fmt.Errorf("count live containers: %w", err)
	}
	workspaces, err := q.OwnedWorkspaceCount(ctx, user)
	if err != nil {
		return apitypes.EntitlementUsage{}, fmt.Errorf("count workspaces: %w", err)
	}
	members, err := q.OwnerMemberCount(ctx, user)
	if err != nil {
		return apitypes.EntitlementUsage{}, fmt.Errorf("count members: %w", err)
	}
	connections, err := q.ConnectedCloudCount(ctx, user)
	if err != nil {
		return apitypes.EntitlementUsage{}, fmt.Errorf("count connected clouds: %w", err)
	}
	domains, err := q.CustomDomainCount(ctx, user)
	if err != nil {
		return apitypes.EntitlementUsage{}, fmt.Errorf("count custom domains: %w", err)
	}
	return apitypes.EntitlementUsage{
		ConcurrentCpuContainers: int(live.CpuContainers), ConcurrentGpus: int(live.Gpus),
		Workspaces: int(workspaces), Members: int(members), ConnectedClouds: int(connections), CustomDomains: int(domains),
	}, nil
}

// SetPreferences sets the monthly usage limit and automatic reload.
// Turning reload on needs a saved card.
func (b *Billing) SetPreferences(ctx context.Context, user uuid.UUID, p apitypes.BillingPreferences) (apitypes.BillingAccount, error) {
	var out apitypes.BillingAccount
	err := pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		if err := ensureAccount(ctx, q, user); err != nil {
			return err
		}
		account, err := q.LockAccount(ctx, user)
		if err != nil {
			return fmt.Errorf("lock account: %w", err)
		}
		if p.ReloadEnabled && account.PaymentMethodAttachedAt == nil {
			return &PaymentRequiredError{Message: "add a payment method before turning on automatic reload"}
		}
		if err := q.SetPreferences(ctx, SetPreferencesParams{
			UserID: user, MonthlyUsageLimitNanos: p.MonthlyUsageLimitNanos, ReloadEnabled: p.ReloadEnabled,
			ReloadThresholdCents: int32(p.ReloadThresholdCents), ReloadAmountCents: int32(p.ReloadAmountCents), //nolint:gosec // The API bounds both to 100000.
		}); err != nil {
			return fmt.Errorf("set preferences: %w", err)
		}
		out, err = viewAccount(ctx, q, user)
		return err
	})
	if err != nil {
		return apitypes.BillingAccount{}, fmt.Errorf("set billing preferences: %w", err)
	}
	return out, nil
}

// ResumeReload resumes automatic reload after a declined or unauthenticated
// payment. The next reload pass charges the card on file again.
func (b *Billing) ResumeReload(ctx context.Context, user uuid.UUID) (apitypes.BillingAccount, error) {
	var out apitypes.BillingAccount
	err := pgx.BeginFunc(ctx, b.pool, func(tx pgx.Tx) error {
		q := b.queries.WithTx(tx)
		if err := ensureAccount(ctx, q, user); err != nil {
			return err
		}
		account, err := q.LockAccount(ctx, user)
		if err != nil {
			return fmt.Errorf("lock account: %w", err)
		}
		if account.PaymentMethodAttachedAt == nil {
			return &PaymentRequiredError{Message: "add a payment method before resuming automatic reload"}
		}
		if err := q.ResumeReload(ctx, user); err != nil {
			return fmt.Errorf("resume reload: %w", err)
		}
		out, err = viewAccount(ctx, q, user)
		return err
	})
	if err != nil {
		return apitypes.BillingAccount{}, fmt.Errorf("resume automatic reload: %w", err)
	}
	return out, nil
}
