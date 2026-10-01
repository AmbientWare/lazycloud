package api

import (
	"context"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// ListUsers pages through every account for a platform administrator.
func (s *Server) ListUsers(ctx context.Context, req ListUsersRequestObject) (ListUsersResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	query := identity.UserQuery{
		Role: (*identity.PlatformRole)(req.Params.Role), Status: (*identity.UserStatus)(req.Params.Status),
		After: (*identity.UserID)(req.Params.Cursor), Limit: 50,
	}
	if req.Params.Search != nil {
		query.Search = *req.Params.Search
	}
	if req.Params.Limit != nil {
		query.Limit = *req.Params.Limit
	}
	page, err := s.owners.Identity.ListUsers(ctx, p, query)
	if err != nil {
		return nil, err
	}
	out := ListUsers200JSONResponse{Users: make([]apitypes.User, len(page.Users)), NextCursor: (*uuid.UUID)(page.Next)}
	for n, u := range page.Users {
		out.Users[n] = userOut(u)
	}
	return out, nil
}

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
