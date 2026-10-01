package api_test

import (
	"net/http"
	"testing"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// token creates a user and returns a token for it; workspace restricts
// the token to one workspace when set.
func (e *env) token(email string, admin bool, workspace string) string {
	e.t.Helper()
	if _, err := e.identity.CreateUser(e.t.Context(), email, admin); err != nil {
		e.t.Fatal(err)
	}
	if workspace != "" {
		if _, err := e.identity.CreateWorkspace(e.t.Context(), workspace, email); err != nil {
			e.t.Fatal(err)
		}
	}
	token, err := e.identity.CreateToken(e.t.Context(), email, workspace, "test")
	if err != nil {
		e.t.Fatal(err)
	}
	return token
}

func TestBillingOperationsActForTheCallersOwnAccount(t *testing.T) {
	e := newEnv(t)
	var failure apitypes.Error

	// The catalog is public and cacheable.
	var catalog apitypes.PricingCatalog
	if status := e.do("GET", "/v1/pricing", "", nil, &catalog); status != http.StatusOK || len(catalog.Plans) != 3 || catalog.Trial.AmountNanos != 2_000_000_000 {
		t.Fatalf("pricing: %d %+v", status, catalog.Plans)
	}

	payer := e.token("payer@example.com", false, "")
	var account apitypes.BillingAccount
	if status := e.do("GET", "/v1/billing", payer, nil, &account); status != http.StatusOK ||
		account.Plan.Id != apitypes.Free || account.BalanceNanos != 2_000_000_000 || account.Entitlements.MaxConcurrentCpuContainers != 10 {
		t.Fatalf("new account: %d %+v", status, account)
	}
	limit := int64(0)
	prefs := apitypes.BillingPreferences{MonthlyUsageLimitNanos: &limit, ReloadThresholdCents: 1000, ReloadAmountCents: 2000}
	if status := e.do("PUT", "/v1/billing/preferences", payer, prefs, &account); status != http.StatusOK ||
		account.UsageBudget.LimitNanos == nil || *account.UsageBudget.LimitNanos != 0 {
		t.Fatalf("set a $0 limit: %d %+v", status, account.UsageBudget)
	}
	prefs.ReloadEnabled = true
	if status := e.do("PUT", "/v1/billing/preferences", payer, prefs, &failure); status != http.StatusPaymentRequired || failure.Code != apitypes.PaymentRequired {
		t.Fatalf("reload without a card: %d %+v", status, failure)
	}
	// Payments need Stripe, which this server lacks.
	if status := e.do("POST", "/v1/billing/payment-method-sessions", payer, apitypes.HostedSessionRequest{ReturnUrl: dashboardURL + "/x"}, &failure); status != http.StatusServiceUnavailable {
		t.Fatalf("card session without Stripe: %d %+v", status, failure)
	}

	// A token restricted to one workspace cannot act for the account.
	restricted := e.token("scoped@example.com", false, "scoped")
	if status := e.do("GET", "/v1/billing", restricted, nil, &failure); status != http.StatusForbidden {
		t.Fatalf("workspace token: %d %+v", status, failure)
	}
	if status := e.do("GET", "/v1/billing", "", nil, &failure); status != http.StatusUnauthorized {
		t.Fatalf("anonymous: %d", status)
	}
}

func TestOnlyAdministratorsSeeAndWaiveAccounts(t *testing.T) {
	e := newEnv(t)
	admin := e.token("admin@example.com", true, "")
	member := e.token("member@example.com", false, "")
	var failure apitypes.Error
	if status := e.do("GET", "/v1/billing/accounts", member, nil, &failure); status != http.StatusForbidden {
		t.Fatalf("member lists accounts: %d %+v", status, failure)
	}
	var page apitypes.BillingAccountAdminPage
	if status := e.do("GET", "/v1/billing/accounts?search=member%40&role=user", admin, nil, &page); status != http.StatusOK ||
		len(page.Accounts) != 1 || page.Accounts[0].User.Email != "member@example.com" {
		t.Fatalf("search: %d %+v", status, page)
	}
	target := page.Accounts[0].User.Id
	path := "/v1/billing/accounts/" + target.String() + "/complimentary"
	if status := e.do("PUT", path, member, apitypes.ComplimentaryRequest{Complimentary: true}, &failure); status != http.StatusForbidden {
		t.Fatalf("member waives: %d", status)
	}
	var waived apitypes.BillingAccountAdmin
	if status := e.do("PUT", path, admin, apitypes.ComplimentaryRequest{Complimentary: true}, &waived); status != http.StatusOK || waived.ComplimentarySince == nil {
		t.Fatalf("admin waives: %d %+v", status, waived)
	}
	var account apitypes.BillingAccount
	if status := e.do("GET", "/v1/billing", member, nil, &account); status != http.StatusOK ||
		account.ComplimentarySince == nil || account.Entitlements.MaxConcurrentCpuContainers != 2000 {
		t.Fatalf("waived account: %d %+v", status, account.Entitlements)
	}
	var unwaived apitypes.BillingAccountAdmin
	if status := e.do("PUT", path, admin, apitypes.ComplimentaryRequest{Complimentary: false}, &unwaived); status != http.StatusOK || unwaived.ComplimentarySince != nil {
		t.Fatalf("admin stops waiving: %d %+v", status, unwaived)
	}
}

func TestStripeDeliveriesNeedTheEndpointSecret(t *testing.T) {
	e := newEnv(t)
	resp, err := http.Post(e.url+"/webhooks/stripe", "application/json", http.NoBody) //nolint:noctx // A one-off test request.
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("delivery to a server without Stripe: %d", resp.StatusCode)
	}
}
