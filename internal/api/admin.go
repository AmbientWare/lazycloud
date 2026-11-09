package api

import (
	"context"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/notifications"
)

// UpdateUser changes an account's platform role or status; the schema
// admits exactly one.
func (s *server) UpdateUser(ctx context.Context, req UpdateUserRequestObject) (UpdateUserResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	var u identity.User
	if req.Body.Role != nil {
		u, err = s.owners.Identity.SetUserRole(ctx, p, identity.UserID(req.User), identity.PlatformRole(*req.Body.Role))
	} else {
		u, err = s.owners.Identity.SetUserStatus(ctx, p, identity.UserID(req.User), identity.UserStatus(*req.Body.Status))
	}
	if err != nil {
		return nil, err
	}
	return UpdateUser200JSONResponse(userOut(u)), nil
}

// ListFeedback pages through every account's feedback for administrators.
func (s *server) ListFeedback(ctx context.Context, req ListFeedbackRequestObject) (ListFeedbackResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	f := identity.FeedbackFilter{Kind: (*identity.FeedbackKind)(req.Params.Kind), Search: valueOr(req.Params.Search, "")}
	page, err := s.owners.Identity.ListFeedback(ctx, p, f, (*identity.FeedbackID)(req.Params.Cursor), valueOr(req.Params.Limit, 50))
	if err != nil {
		return nil, err
	}
	out := ListFeedback200JSONResponse{Feedback: make([]apitypes.Feedback, len(page.Feedback)), NextCursor: (*uuid.UUID)(page.Next)}
	for n, f := range page.Feedback {
		out.Feedback[n] = apitypes.Feedback{
			Id: uuid.UUID(f.ID), Kind: apitypes.FeedbackKind(f.Kind), Message: f.Message, Author: userOut(f.Author), CreatedAt: f.CreatedAt,
		}
	}
	return out, nil
}

// DeleteFeedback removes a piece of feedback for administrators.
func (s *server) DeleteFeedback(ctx context.Context, req DeleteFeedbackRequestObject) (DeleteFeedbackResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Identity.DeleteFeedback(ctx, p, identity.FeedbackID(req.Feedback)); err != nil {
		return nil, err
	}
	return DeleteFeedback204Response{}, nil
}

// SubmitFeedback records the caller's feedback.
func (s *server) SubmitFeedback(ctx context.Context, req SubmitFeedbackRequestObject) (SubmitFeedbackResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Identity.SubmitFeedback(ctx, p, identity.FeedbackKind(req.Body.Kind), req.Body.Message); err != nil {
		return nil, err
	}
	return SubmitFeedback204Response{}, nil
}

// GetAdminAlerts returns where administrator alerts go and the spend
// limits that raise them.
func (s *server) GetAdminAlerts(ctx context.Context, _ GetAdminAlertsRequestObject) (GetAdminAlertsResponseObject, error) {
	if err := s.admin(ctx); err != nil {
		return nil, err
	}
	a, err := s.owners.Notifications.AdminAlerts(ctx)
	if err != nil {
		return nil, err
	}
	return GetAdminAlerts200JSONResponse(adminAlertsOut(a)), nil
}

// SetAdminAlerts replaces the alert settings.
func (s *server) SetAdminAlerts(ctx context.Context, req SetAdminAlertsRequestObject) (SetAdminAlertsResponseObject, error) {
	if err := s.admin(ctx); err != nil {
		return nil, err
	}
	a := notifications.AdminAlerts{
		Email: req.Body.Email, HourlyLimitMicros: req.Body.HourlyLimitMicros, DailyLimitMicros: req.Body.DailyLimitMicros,
	}
	if err := s.owners.Notifications.SetAdminAlerts(ctx, a); err != nil {
		return nil, err
	}
	return SetAdminAlerts200JSONResponse(adminAlertsOut(a)), nil
}

func adminAlertsOut(a notifications.AdminAlerts) apitypes.AdminAlerts {
	return apitypes.AdminAlerts{Email: a.Email, HourlyLimitMicros: a.HourlyLimitMicros, DailyLimitMicros: a.DailyLimitMicros}
}
