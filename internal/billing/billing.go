// Package billing owns plans and prices, credit and balances, spending
// controls, usage metering, the ledger and the Stripe operations behind
// payments. A workspace's billing account is its owner's user. Other owners
// call Admit and the plan-limit checks inside their own transactions.
package billing

import (
	"cmp"
	"errors"
	"fmt"
	"log/slog"
	"strings"

	"github.com/jackc/pgx/v5/pgxpool"
)

//go:generate go tool sqlc generate

// PaymentRequiredError refuses work the account must pay for first: no
// credit left, the monthly usage limit reached, a payment past due, or a
// capability its plan or a missing card does not include.
type PaymentRequiredError struct{ Message string }

func (e *PaymentRequiredError) Error() string { return e.Message }

// LimitError refuses something the account already holds the most of its
// plan allows. The message names the limit and what is held.
type LimitError struct{ Message string }

func (e *LimitError) Error() string { return e.Message }

// ConflictError is a request the account's current state refuses.
type ConflictError struct{ Message string }

func (e *ConflictError) Error() string { return e.Message }

// GPUUnavailableError refuses work that accepts only GPU models the
// platform does not offer yet.
type GPUUnavailableError struct{ Models []GPUType }

func (e *GPUUnavailableError) Error() string {
	verb := "is"
	if len(e.Models) > 1 {
		verb = "are"
	}
	return fmt.Sprintf("%s %s coming soon and cannot be requested yet; the GPU models available now are %s",
		joinModels(e.Models), verb, joinModels(enabledGPUs()))
}

// InvalidError is a request billing rejects.
type InvalidError struct{ Message string }

func (e *InvalidError) Error() string { return e.Message }

var (
	// ErrNotFound means the purchase or account does not exist for the
	// caller.
	ErrNotFound = errors.New("not found")
	// ErrPaymentsUnavailable means Stripe is not configured, so nothing
	// that needs it can run.
	ErrPaymentsUnavailable = errors.New("payments are not configured on this platform")
)

// Config is what billing needs beyond the database.
type Config struct {
	// PublicURL is the dashboard origin; Stripe's pages may only send
	// people back under it.
	PublicURL string
	Stripe    StripeConfig
	// SessionPool holds the metering lock, a session advisory lock
	// (database.OpenSession); nil uses the pool.
	SessionPool *pgxpool.Pool
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
	b := &Billing{pool: pool, session: cmp.Or(cfg.SessionPool, pool), queries: New(pool), rates: newRates(), cfg: cfg, logger: logger}
	if cfg.Stripe.SecretKey != "" {
		b.stripe = newStripeProvider(cfg.Stripe)
	}
	return b
}
