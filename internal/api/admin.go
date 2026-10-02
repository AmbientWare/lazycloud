package api

import (
	"context"

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
