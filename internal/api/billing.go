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
)

// GetPricing returns the public pricing catalog.
func (s *server) GetPricing(_ context.Context, req GetPricingRequestObject) (GetPricingResponseObject, error) {
	catalog, err := s.owners.Billing.Catalog(valueOr(req.Params.At, time.Now()))
	if err != nil {
		return nil, err
	}
	cache := "public, max-age=300"
	return GetPricing200JSONResponse{Body: catalog, Headers: GetPricing200ResponseHeaders{CacheControl: &cache}}, nil
}

// GetBilling returns the caller's billing account.
func (s *server) GetBilling(ctx context.Context, _ GetBillingRequestObject) (GetBillingResponseObject, error) {
	p, err := s.account(ctx, "manage billing")
	if err != nil {
		return nil, err
	}
	account, err := s.owners.Billing.Account(ctx, uuid.UUID(p.User))
	if err != nil {
		return nil, err
	}
	return GetBilling200JSONResponse(account), nil
}

// SetBillingPreferences sets the monthly usage limit and automatic reload.
func (s *server) SetBillingPreferences(ctx context.Context, req SetBillingPreferencesRequestObject) (SetBillingPreferencesResponseObject, error) {
	p, err := s.account(ctx, "manage billing")
	if err != nil {
		return nil, err
	}
	account, err := s.owners.Billing.SetPreferences(ctx, uuid.UUID(p.User), *req.Body)
	if err != nil {
		return nil, err
	}
	return SetBillingPreferences200JSONResponse(account), nil
}

// ResumeAutomaticReload resumes reload after a failed payment.
func (s *server) ResumeAutomaticReload(ctx context.Context, _ ResumeAutomaticReloadRequestObject) (ResumeAutomaticReloadResponseObject, error) {
	p, err := s.account(ctx, "manage billing")
	if err != nil {
		return nil, err
	}
	account, err := s.owners.Billing.ResumeReload(ctx, uuid.UUID(p.User))
	if err != nil {
		return nil, err
	}
	return ResumeAutomaticReload200JSONResponse(account), nil
}

// ChangePlan moves the account onto published plan terms.
func (s *server) ChangePlan(ctx context.Context, req ChangePlanRequestObject) (ChangePlanResponseObject, error) {
	p, err := s.account(ctx, "manage billing")
	if err != nil {
		return nil, err
	}
	account, err := s.owners.Billing.ChangePlan(ctx, uuid.UUID(p.User), *req.Body)
	if err != nil {
		return nil, err
	}
	return ChangePlan200JSONResponse(account), nil
}

// StartPaymentMethodSetup opens Stripe's card page.
func (s *server) StartPaymentMethodSetup(ctx context.Context, req StartPaymentMethodSetupRequestObject) (StartPaymentMethodSetupResponseObject, error) {
	p, err := s.account(ctx, "manage billing")
	if err != nil {
		return nil, err
	}
	url, err := s.owners.Billing.PaymentMethodSession(ctx, uuid.UUID(p.User), *req.Body)
	if err != nil {
		return nil, err
	}
	return StartPaymentMethodSetup201JSONResponse{Url: url}, nil
}

// StartBillingPortal opens Stripe's invoices page.
func (s *server) StartBillingPortal(ctx context.Context, req StartBillingPortalRequestObject) (StartBillingPortalResponseObject, error) {
	p, err := s.account(ctx, "manage billing")
	if err != nil {
		return nil, err
	}
	url, err := s.owners.Billing.PortalSession(ctx, uuid.UUID(p.User), *req.Body)
	if err != nil {
		return nil, err
	}
	return StartBillingPortal201JSONResponse{Url: url}, nil
}

// CreateCreditPurchase starts a credit purchase through Checkout.
func (s *server) CreateCreditPurchase(ctx context.Context, req CreateCreditPurchaseRequestObject) (CreateCreditPurchaseResponseObject, error) {
	p, err := s.account(ctx, "manage billing")
	if err != nil {
		return nil, err
	}
	purchase, err := s.owners.Billing.BuyCredit(ctx, uuid.UUID(p.User), *req.Body)
	if err != nil {
		return nil, err
	}
	return CreateCreditPurchase201JSONResponse(purchase), nil
}

// ListCosts pages through the account's usage cost.
func (s *server) ListCosts(ctx context.Context, req ListCostsRequestObject) (ListCostsResponseObject, error) {
	p, err := s.account(ctx, "manage billing")
	if err != nil {
		return nil, err
	}
	q := billing.CostQuery{
		Start: req.Params.Start, End: req.Params.End, GroupBy: valueOr(req.Params.GroupBy, apitypes.UsageCostGroupApp),
		Workspace: req.Params.WorkspaceId, App: req.Params.AppId, Workload: req.Params.WorkloadId, Category: req.Params.Category,
		Cursor: valueOr(req.Params.Cursor, ""), Limit: valueOr(req.Params.Limit, 50),
	}
	page, err := s.owners.Billing.Costs(ctx, uuid.UUID(p.User), q)
	if err != nil {
		return nil, err
	}
	return ListCosts200JSONResponse(page), nil
}

// GetCostSeries returns the account's spend interval by interval.
func (s *server) GetCostSeries(ctx context.Context, req GetCostSeriesRequestObject) (GetCostSeriesResponseObject, error) {
	p, err := s.account(ctx, "manage billing")
	if err != nil {
		return nil, err
	}
	series, err := s.owners.Billing.CostSeries(ctx, uuid.UUID(p.User), req.Params.Start, req.Params.End, valueOr(req.Params.Bucket, apitypes.Day))
	if err != nil {
		return nil, err
	}
	return GetCostSeries200JSONResponse(series), nil
}

// ListBillingAccounts lists every user's billing standing for
// administrators.
func (s *server) ListBillingAccounts(ctx context.Context, req ListBillingAccountsRequestObject) (ListBillingAccountsResponseObject, error) {
	if err := s.admin(ctx); err != nil {
		return nil, err
	}
	f := billing.AccountFilter{
		Status: req.Params.Status, Search: valueOr(req.Params.Search, ""), Cursor: valueOr(req.Params.Cursor, ""), Limit: valueOr(req.Params.Limit, 50),
	}
	if req.Params.Role != nil {
		admin := *req.Params.Role == apitypes.PlatformRoleAdministrator
		f.Admin = &admin
	}
	page, err := s.owners.Billing.Accounts(ctx, f)
	if err != nil {
		return nil, err
	}
	return ListBillingAccounts200JSONResponse(page), nil
}

// SetComplimentary waives a user's usage charges or stops waiving them.
func (s *server) SetComplimentary(ctx context.Context, req SetComplimentaryRequestObject) (SetComplimentaryResponseObject, error) {
	if err := s.admin(ctx); err != nil {
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
func (s *server) receiveStripeWebhook(w http.ResponseWriter, r *http.Request) {
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
