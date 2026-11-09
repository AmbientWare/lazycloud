// Package billing owns plans and prices, credit and balances, spending
// controls, usage metering, the ledger and the Stripe operations behind
// payments. A workspace's billing account is its owner's user. Other owners
// call Admit and the plan-limit checks inside their own transactions.
package billing

import (
	"fmt"
	"log/slog"
	"os"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

//go:generate go tool sqlc generate

// gpuUnavailable refuses work only the platform fleet can serve that
// accepts only GPU models the fleet does not offer.
func gpuUnavailable(models []GPUType) error {
	verb, pronoun := "is", "it"
	if len(models) > 1 {
		verb, pronoun = "are", "them"
	}
	return &apitypes.Error{Code: apitypes.Unsupported, Message: fmt.Sprintf(
		"%s %s coming soon to the platform fleet, which offers %s; a joined machine or a connected AWS account can run %s now",
		joinModels(models), verb, joinModels(enabledGPUs()), pronoun)}
}

var (
	// ErrNotFound means the purchase or account does not exist for the
	// caller.
	ErrNotFound error = &apitypes.Error{Code: apitypes.NotFound, Message: "not found"}
	// ErrPaymentsUnavailable means Stripe is not configured, so nothing
	// that needs it can run.
	ErrPaymentsUnavailable error = &apitypes.Error{Code: apitypes.Unavailable, Message: "payments are not configured on this platform"}
)

// Config is what billing needs beyond the database.
type Config struct {
	// PublicURL is the dashboard origin; Stripe's pages may only send
	// people back under it.
	PublicURL string
	Stripe    StripeConfig
	// SessionPool holds the metering lock, a session advisory lock
	// (database.OpenSession); Meter needs it.
	SessionPool *pgxpool.Pool
}

// ConfigFromEnv reads the Stripe credentials from the environment, so they
// stay out of the process arguments.
func ConfigFromEnv(publicURL string, session *pgxpool.Pool) Config {
	return Config{PublicURL: publicURL, SessionPool: session, Stripe: StripeConfig{
		SecretKey: os.Getenv("LAZYCLOUD_STRIPE_API_KEY"), WebhookSecret: os.Getenv("LAZYCLOUD_STRIPE_WEBHOOK_SECRET"),
	}}
}

// Billing is the billing owner.
type Billing struct {
	pool    *pgxpool.Pool
	session *pgxpool.Pool
	queries *Queries
	rates   rates
	stripe  *stripeProvider
	cfg     Config
	logger  *slog.Logger
}

// NewBilling returns the billing owner. Without a Stripe secret key the
// account, credit, metering and admission work and every payment operation
// answers ErrPaymentsUnavailable.
func NewBilling(pool *pgxpool.Pool, cfg Config, logger *slog.Logger) *Billing {
	cfg.PublicURL = strings.TrimRight(cfg.PublicURL, "/")
	b := &Billing{pool: pool, session: cfg.SessionPool, queries: New(pool), rates: newRates(), cfg: cfg, logger: logger}
	if cfg.Stripe.SecretKey != "" {
		b.stripe = newStripeProvider(cfg.Stripe)
	}
	return b
}
