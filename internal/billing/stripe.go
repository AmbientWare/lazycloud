package billing

// StripeConfig configures the Stripe provider.
type StripeConfig struct {
	SecretKey     string
	WebhookSecret string
	// APIBase overrides api.stripe.com, for stripe-mock in tests.
	APIBase string
}

type stripeProvider struct{ cfg StripeConfig }

func newStripeProvider(cfg StripeConfig) *stripeProvider { return &stripeProvider{cfg: cfg} }
