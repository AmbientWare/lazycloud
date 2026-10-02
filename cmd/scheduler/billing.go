package main

import (
	"context"
	"log/slog"
	"os"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/sync/errgroup"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

const (
	// meteringTick paces metering, balance rollups and enforcement, so a
	// balance trails usage by about this much.
	meteringTick = 15 * time.Second
	// paymentsTick paces Stripe deliveries, purchases and plan changes.
	paymentsTick = 5 * time.Second
	// retentionTick paces unfunded data retention, which is counted in
	// days.
	retentionTick = time.Minute
)

// billingLoops meter usage, roll up balances, stop work accounts may not
// pay for, reload credit and settle Stripe's deliveries.
type billingLoops struct {
	billing   *billing.Billing
	execution *execution.Execution
	storage   *storage.Storage
	logger    *slog.Logger
}

// newBillingLoops reads the Stripe credentials from the environment;
// without them payments are off and metering still runs.
func newBillingLoops(pool, session *pgxpool.Pool, exec *execution.Execution, store *storage.Storage, logger *slog.Logger) *billingLoops {
	cfg := billing.Config{Stripe: billing.StripeConfig{
		SecretKey: os.Getenv("LAZYCLOUD_STRIPE_API_KEY"), WebhookSecret: os.Getenv("LAZYCLOUD_STRIPE_WEBHOOK_SECRET"),
	}, SessionPool: session}
	if cfg.Stripe.SecretKey == "" {
		logger.Warn("payments are off: set LAZYCLOUD_STRIPE_API_KEY; usage is metered against credit only")
	}
	return &billingLoops{billing: billing.NewBilling(pool, cfg, logger), execution: exec, storage: store, logger: logger}
}

func (b *billingLoops) start(ctx context.Context, group *errgroup.Group, p *pace) {
	group.Go(func() error {
		return p.loop(ctx, cadence{every: meteringTick}, nil, nil, func(ctx context.Context) bool {
			b.meter(ctx)
			return false
		})
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: paymentsTick}, nil, nil, func(ctx context.Context) bool {
			b.payments(ctx)
			return false
		})
	})
	group.Go(func() error {
		return p.loop(ctx, cadence{every: retentionTick}, nil, nil, func(ctx context.Context) bool {
			b.retain(ctx)
			return false
		})
	})
}

// retain starts and ends unfunded retention periods and deletes the data of
// workspaces whose period passed without credit.
func (b *billingLoops) retain(ctx context.Context) {
	if _, err := b.billing.SweepRetention(ctx); err != nil {
		b.logger.ErrorContext(ctx, "unfunded retention", "error", err)
	}
	expired, err := b.billing.ExpiredUnfunded(ctx)
	if err != nil {
		b.logger.ErrorContext(ctx, "expired retention", "error", err)
	}
	for _, account := range expired {
		remaining := false
		for _, ws := range account.Workspaces {
			deleted, err := b.storage.DeleteUnfunded(ctx, identity.WorkspaceID(ws))
			if err != nil {
				b.logger.ErrorContext(ctx, "delete unfunded data", "workspace_id", ws, "error", err)
				remaining = true
				continue
			}
			if deleted > 0 {
				remaining = true
				b.logger.WarnContext(ctx, "deleted unfunded data", "user_id", account.User, "workspace_id", ws, "items", deleted)
			}
		}
		if !remaining {
			if err := b.billing.EndRetention(ctx, account.User); err != nil {
				b.logger.ErrorContext(ctx, "end retention", "user_id", account.User, "error", err)
			}
		}
	}
}

// meter runs one metering pass, rolls up every due balance, stops the
// containers of accounts that may not run them and starts automatic
// reloads. Each step logs its own failure and the next still runs.
func (b *billingLoops) meter(ctx context.Context) {
	if _, err := b.billing.Meter(ctx); err != nil {
		b.logger.ErrorContext(ctx, "metering pass", "error", err)
	}
	var rollupErr error
	for {
		result, err := b.billing.Rollup(ctx)
		if err != nil {
			rollupErr = err
			b.logger.ErrorContext(ctx, "balance rollup", "error", err)
		}
		if err != nil || !result.More {
			break
		}
	}
	unfunded, err := b.billing.UnfundedAccounts(ctx)
	if err != nil {
		b.logger.ErrorContext(ctx, "unfunded accounts", "error", err)
	}
	for _, account := range unfunded {
		for _, ws := range account.Workspaces {
			stopped, err := b.execution.StopUnfunded(ctx, identity.WorkspaceID(ws), account.Reason)
			if err != nil {
				b.logger.ErrorContext(ctx, "stop unfunded containers", "user_id", account.User, "workspace_id", ws, "error", err)
				continue
			}
			if stopped > 0 {
				b.logger.WarnContext(ctx, "stopped unfunded containers", "user_id", account.User, "workspace_id", ws,
					"count", stopped, "reason", account.Reason)
			}
		}
	}
	// A reload decides from settled balances; without a rollup it waits.
	if rollupErr != nil {
		return
	}
	if _, err := b.billing.Reload(ctx); err != nil {
		b.logger.ErrorContext(ctx, "automatic reload", "error", err)
	}
}

func (b *billingLoops) payments(ctx context.Context) {
	if _, err := b.billing.ProcessEvents(ctx); err != nil {
		b.logger.ErrorContext(ctx, "stripe deliveries", "error", err)
	}
	if _, err := b.billing.SweepPurchases(ctx); err != nil {
		b.logger.ErrorContext(ctx, "credit purchases", "error", err)
	}
	if _, err := b.billing.SettlePlanChanges(ctx); err != nil {
		b.logger.ErrorContext(ctx, "plan changes", "error", err)
	}
}
