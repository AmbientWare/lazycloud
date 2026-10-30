package api

import (
	"context"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// SetUserRole grants or withdraws platform administration.
func (s *Server) SetUserRole(ctx context.Context, req SetUserRoleRequestObject) (SetUserRoleResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	u, err := s.owners.Identity.SetUserRole(ctx, p, identity.UserID(req.User), identity.PlatformRole(req.Body.Role))
	if err != nil {
		return nil, err
	}
	return SetUserRole200JSONResponse(userOut(u)), nil
}

// SetUserStatus enables or disables an account.
func (s *Server) SetUserStatus(ctx context.Context, req SetUserStatusRequestObject) (SetUserStatusResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	u, err := s.owners.Identity.SetUserStatus(ctx, p, identity.UserID(req.User), identity.UserStatus(req.Body.Status))
	if err != nil {
		return nil, err
	}
	return SetUserStatus200JSONResponse(userOut(u)), nil
}

// ListFeedback pages through every account's feedback for administrators.
func (s *Server) ListFeedback(ctx context.Context, req ListFeedbackRequestObject) (ListFeedbackResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	limit := 50
	if req.Params.Limit != nil {
		limit = *req.Params.Limit
	}
	f := identity.FeedbackFilter{Kind: (*identity.FeedbackKind)(req.Params.Kind)}
	if req.Params.Search != nil {
		f.Search = *req.Params.Search
	}
	page, err := s.owners.Identity.ListFeedback(ctx, p, f, (*identity.FeedbackID)(req.Params.Cursor), limit)
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
func (s *Server) DeleteFeedback(ctx context.Context, req DeleteFeedbackRequestObject) (DeleteFeedbackResponseObject, error) {
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
func (s *Server) SubmitFeedback(ctx context.Context, req SubmitFeedbackRequestObject) (SubmitFeedbackResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Identity.SubmitFeedback(ctx, p, identity.FeedbackKind(req.Body.Kind), req.Body.Message); err != nil {
		return nil, err
	}
	return SubmitFeedback204Response{}, nil
}
