package api

import (
	"context"
	"errors"
	"io"
	"net/http"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// payer is the account a billing operation acts for: the caller's own,
// reached only with an account credential.
func (s *Server) payer(ctx context.Context) (uuid.UUID, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return uuid.UUID{}, err
	}
	if p.TokenWorkspace != nil {
		return uuid.UUID{}, &identity.AccountError{Action: "manage billing"}
	}
	return uuid.UUID(p.User), nil
}

// billingAdmin admits platform administrators with an account credential.
func (s *Server) billingAdmin(ctx context.Context) error {
	p, err := s.principal(ctx)
	if err != nil {
		return err
	}
	if p.TokenWorkspace != nil {
		return &identity.AccountError{Action: "administer billing"}
	}
	if !p.IsAdmin {
		return identity.ErrAdminRequired
	}
	return nil
}

// GetPricing returns the public pricing catalog.
func (s *Server) GetPricing(_ context.Context, req GetPricingRequestObject) (GetPricingResponseObject, error) {
	at := time.Now()
	if req.Params.At != nil {
		at = *req.Params.At
	}
	catalog, err := s.owners.Billing.Catalog(at)
	if err != nil {
		return nil, err
	}
	cache := "public, max-age=300"
	return GetPricing200JSONResponse{Body: catalog, Headers: GetPricing200ResponseHeaders{CacheControl: &cache}}, nil
}

// GetBilling returns the caller's billing account.
func (s *Server) GetBilling(ctx context.Context, _ GetBillingRequestObject) (GetBillingResponseObject, error) {
	user, err := s.payer(ctx)
	if err != nil {
		return nil, err
	}
	account, err := s.owners.Billing.Account(ctx, user)
	if err != nil {
		return nil, err
	}
	return GetBilling200JSONResponse(account), nil
}

// SetBillingPreferences sets the monthly usage limit and automatic reload.
func (s *Server) SetBillingPreferences(ctx context.Context, req SetBillingPreferencesRequestObject) (SetBillingPreferencesResponseObject, error) {
	user, err := s.payer(ctx)
	if err != nil {
		return nil, err
	}
	account, err := s.owners.Billing.SetPreferences(ctx, user, *req.Body)
	if err != nil {
		return nil, err
	}
	return SetBillingPreferences200JSONResponse(account), nil
}

// ResumeAutomaticReload resumes reload after a failed payment.
func (s *Server) ResumeAutomaticReload(ctx context.Context, _ ResumeAutomaticReloadRequestObject) (ResumeAutomaticReloadResponseObject, error) {
	user, err := s.payer(ctx)
	if err != nil {
		return nil, err
	}
	account, err := s.owners.Billing.ResumeReload(ctx, user)
	if err != nil {
		return nil, err
	}
	return ResumeAutomaticReload200JSONResponse(account), nil
}

// ChangePlan moves the account onto published plan terms.
func (s *Server) ChangePlan(ctx context.Context, req ChangePlanRequestObject) (ChangePlanResponseObject, error) {
	user, err := s.payer(ctx)
	if err != nil {
		return nil, err
	}
	account, err := s.owners.Billing.ChangePlan(ctx, user, *req.Body)
	if err != nil {
		return nil, err
	}
	return ChangePlan200JSONResponse(account), nil
}

// StartPaymentMethodSetup opens Stripe's card page.
func (s *Server) StartPaymentMethodSetup(ctx context.Context, req StartPaymentMethodSetupRequestObject) (StartPaymentMethodSetupResponseObject, error) {
	user, err := s.payer(ctx)
	if err != nil {
		return nil, err
	}
	url, err := s.owners.Billing.PaymentMethodSession(ctx, user, *req.Body)
	if err != nil {
		return nil, err
	}
	return StartPaymentMethodSetup201JSONResponse{Url: url}, nil
}

// StartBillingPortal opens Stripe's invoices page.
func (s *Server) StartBillingPortal(ctx context.Context, req StartBillingPortalRequestObject) (StartBillingPortalResponseObject, error) {
	user, err := s.payer(ctx)
	if err != nil {
		return nil, err
	}
	url, err := s.owners.Billing.PortalSession(ctx, user, *req.Body)
	if err != nil {
		return nil, err
	}
	return StartBillingPortal201JSONResponse{Url: url}, nil
}

// CreateCreditPurchase starts a credit purchase through Checkout.
func (s *Server) CreateCreditPurchase(ctx context.Context, req CreateCreditPurchaseRequestObject) (CreateCreditPurchaseResponseObject, error) {
	user, err := s.payer(ctx)
	if err != nil {
		return nil, err
	}
	purchase, err := s.owners.Billing.BuyCredit(ctx, user, *req.Body)
	if err != nil {
		return nil, err
	}
	return CreateCreditPurchase201JSONResponse(purchase), nil
}

// GetCreditPurchase returns one of the caller's purchases.
func (s *Server) GetCreditPurchase(ctx context.Context, req GetCreditPurchaseRequestObject) (GetCreditPurchaseResponseObject, error) {
	user, err := s.payer(ctx)
	if err != nil {
		return nil, err
	}
	purchase, err := s.owners.Billing.CreditPurchase(ctx, user, req.Purchase)
	if err != nil {
		return nil, err
	}
	return GetCreditPurchase200JSONResponse(purchase), nil
}

// ListCosts pages through the account's usage cost.
func (s *Server) ListCosts(ctx context.Context, req ListCostsRequestObject) (ListCostsResponseObject, error) {
	user, err := s.payer(ctx)
	if err != nil {
		return nil, err
	}
	q := billing.CostQuery{
		Start: req.Params.Start, End: req.Params.End, GroupBy: apitypes.UsageCostGroupApp,
		Workspace: req.Params.WorkspaceId, App: req.Params.AppId, Category: req.Params.Category, Limit: 50,
	}
	if req.Params.GroupBy != nil {
		q.GroupBy = *req.Params.GroupBy
	}
	if req.Params.Cursor != nil {
		q.Cursor = *req.Params.Cursor
	}
	if req.Params.Limit != nil {
		q.Limit = *req.Params.Limit
	}
	page, err := s.owners.Billing.Costs(ctx, user, q)
	if err != nil {
		return nil, err
	}
	return ListCosts200JSONResponse(page), nil
}

// GetCostSeries returns the account's spend interval by interval.
func (s *Server) GetCostSeries(ctx context.Context, req GetCostSeriesRequestObject) (GetCostSeriesResponseObject, error) {
	user, err := s.payer(ctx)
	if err != nil {
		return nil, err
	}
	bucket := apitypes.Day
	if req.Params.Bucket != nil {
		bucket = *req.Params.Bucket
	}
	series, err := s.owners.Billing.CostSeries(ctx, user, req.Params.Start, req.Params.End, bucket)
	if err != nil {
		return nil, err
	}
	return GetCostSeries200JSONResponse(series), nil
}

// ListBillingAccounts lists every user's billing standing for
// administrators.
func (s *Server) ListBillingAccounts(ctx context.Context, req ListBillingAccountsRequestObject) (ListBillingAccountsResponseObject, error) {
	if err := s.billingAdmin(ctx); err != nil {
		return nil, err
	}
	f := billing.AccountFilter{Status: req.Params.Status, Limit: 50}
	if req.Params.Search != nil {
		f.Search = *req.Params.Search
	}
	if req.Params.Role != nil {
		admin := *req.Params.Role == apitypes.PlatformRoleAdmin
		f.Admin = &admin
	}
	if req.Params.Cursor != nil {
		f.Cursor = *req.Params.Cursor
	}
	if req.Params.Limit != nil {
		f.Limit = *req.Params.Limit
	}
	page, err := s.owners.Billing.Accounts(ctx, f)
	if err != nil {
		return nil, err
	}
	return ListBillingAccounts200JSONResponse(page), nil
}

// SetComplimentary waives a user's usage charges or stops waiving them.
func (s *Server) SetComplimentary(ctx context.Context, req SetComplimentaryRequestObject) (SetComplimentaryResponseObject, error) {
	if err := s.billingAdmin(ctx); err != nil {
		return nil, err
	}
	account, err := s.owners.Billing.SetComplimentary(ctx, req.User, req.Body.Complimentary)
	if err != nil {
		return nil, err
	}
	return SetComplimentary200JSONResponse(account), nil
}

// receiveStripeWebhook stores a verified Stripe delivery; the scheduler
// processes it. The caller is Stripe, which holds no token: the signature
// stands in for one.
func (s *Server) receiveStripeWebhook(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxWebhookBytes))
	if err != nil {
		writeJSONError(w, http.StatusRequestEntityTooLarge, apitypes.PayloadTooLarge, "the delivery is too large")
		return
	}
	err = s.owners.Billing.ReceiveWebhook(r.Context(), body, r.Header.Get("Stripe-Signature"))
	switch {
	case errors.Is(err, billing.ErrBadSignature):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, "the delivery is not signed by Stripe")
	case errors.Is(err, billing.ErrPaymentsUnavailable):
		s.logger.ErrorContext(r.Context(), "a stripe delivery arrived but LAZYCLOUD_STRIPE_API_KEY or LAZYCLOUD_STRIPE_WEBHOOK_SECRET is not set")
		writeJSONError(w, http.StatusServiceUnavailable, apitypes.Unavailable, "payments are not configured")
	case err != nil:
		s.writeError(w, r, err)
	default:
		w.WriteHeader(http.StatusNoContent)
	}
}
