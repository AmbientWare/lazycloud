package api

import (
	"context"
	"net/http"
	"net/url"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// ActivatePath is the dashboard page where a person approves a device-code
// login.
const ActivatePath = "/activate"

func (s *Server) principal(ctx context.Context) (identity.Principal, error) {
	// A container acts for its workspace, not for a user or account.
	if _, ok := containerFrom(ctx); ok {
		return identity.Principal{}, identity.ErrForbidden
	}
	p, ok := principalFrom(ctx)
	if !ok {
		return identity.Principal{}, identity.ErrUnauthenticated
	}
	return p, nil
}

// GetMe returns the caller and the workspaces they are a member of.
func (s *Server) GetMe(ctx context.Context, _ GetMeRequestObject) (GetMeResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	user, err := s.owners.Identity.Me(ctx, p)
	if err != nil {
		return nil, err
	}
	workspaces, err := s.owners.Identity.Workspaces(ctx, p)
	if err != nil {
		return nil, err
	}
	return GetMe200JSONResponse{User: userOut(user), Workspaces: workspacesOut(workspaces)}, nil
}

// SignOut ends the requesting browser session and clears its cookie.
func (s *Server) SignOut(ctx context.Context, _ SignOutRequestObject) (SignOutResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Identity.SignOut(ctx, p); err != nil {
		return nil, err
	}
	return signedOut{}, nil
}

type signedOut struct{}

func (signedOut) VisitSignOutResponse(w http.ResponseWriter) error {
	http.SetCookie(w, expiredCookie(SessionCookie))
	w.WriteHeader(http.StatusNoContent)
	return nil
}

// ListTokens pages through the caller's tokens.
func (s *Server) ListTokens(ctx context.Context, req ListTokensRequestObject) (ListTokensResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	includeDevice, limit := true, 50
	if req.Params.IncludeDevice != nil {
		includeDevice = *req.Params.IncludeDevice
	}
	if req.Params.Limit != nil {
		limit = *req.Params.Limit
	}
	page, err := s.owners.Identity.ListTokens(ctx, p, includeDevice, (*identity.TokenID)(req.Params.Cursor), limit)
	if err != nil {
		return nil, err
	}
	out := ListTokens200JSONResponse{Tokens: make([]apitypes.Token, len(page.Tokens)), NextCursor: (*uuid.UUID)(page.Next)}
	now := time.Now()
	for n, token := range page.Tokens {
		out.Tokens[n] = tokenOut(token, now)
	}
	return out, nil
}

// CreateToken mints an account token.
func (s *Server) CreateToken(ctx context.Context, req CreateTokenRequestObject) (CreateTokenResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	var lifetime *time.Duration
	if req.Body.ExpiresInSeconds != nil {
		d := time.Duration(*req.Body.ExpiresInSeconds) * time.Second
		lifetime = &d
	}
	secret, token, err := s.owners.Identity.CreateAccountToken(ctx, p, req.Body.Name, lifetime)
	if err != nil {
		return nil, err
	}
	return CreateToken201JSONResponse{Token: secret, Record: tokenOut(token, time.Now())}, nil
}

// RevokeToken revokes one of the caller's tokens.
func (s *Server) RevokeToken(ctx context.Context, req RevokeTokenRequestObject) (RevokeTokenResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Identity.RevokeToken(ctx, p, identity.TokenID(req.Token)); err != nil {
		return nil, err
	}
	return RevokeToken204Response{}, nil
}

// StartDeviceLogin opens a device-code login; it needs no credential.
func (s *Server) StartDeviceLogin(ctx context.Context, req StartDeviceLoginRequestObject) (StartDeviceLoginResponseObject, error) {
	client := ""
	if req.Body.ClientName != nil {
		client = *req.Body.ClientName
	}
	start, err := s.owners.Identity.StartDeviceLogin(ctx, client)
	if err != nil {
		return nil, err
	}
	verification := s.cfg.PublicURL + ActivatePath
	return StartDeviceLogin201JSONResponse{
		DeviceCode: start.DeviceCode, UserCode: start.UserCode, VerificationUri: verification,
		VerificationUriComplete: verification + "?code=" + url.QueryEscape(start.UserCode),
		ExpiresInSeconds:        int(start.ExpiresIn / time.Second),
		PollIntervalSeconds:     int(start.Interval / time.Second),
	}, nil
}

// PollDeviceLogin answers a CLI polling its device code.
func (s *Server) PollDeviceLogin(ctx context.Context, req PollDeviceLoginRequestObject) (PollDeviceLoginResponseObject, error) {
	poll, err := s.owners.Identity.PollDeviceLogin(ctx, req.Body.DeviceCode)
	if err != nil {
		return nil, err
	}
	out := PollDeviceLogin200JSONResponse{
		Status: apitypes.DeviceTokenStatus(poll.Status), PollIntervalSeconds: int(poll.Interval / time.Second),
	}
	if poll.Token != "" {
		out.Token = &poll.Token
	}
	return out, nil
}

// GetDeviceLogin reads the login a user code names.
func (s *Server) GetDeviceLogin(ctx context.Context, req GetDeviceLoginRequestObject) (GetDeviceLoginResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	code, err := s.owners.Identity.DeviceLogin(ctx, p, req.UserCode)
	if err != nil {
		return nil, err
	}
	return GetDeviceLogin200JSONResponse(deviceCodeOut(code)), nil
}

// ApproveDeviceLogin approves a waiting CLI for the caller's account.
func (s *Server) ApproveDeviceLogin(ctx context.Context, req ApproveDeviceLoginRequestObject) (ApproveDeviceLoginResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	code, err := s.owners.Identity.ApproveDeviceLogin(ctx, p, req.UserCode)
	if err != nil {
		return nil, err
	}
	return ApproveDeviceLogin200JSONResponse(deviceCodeOut(code)), nil
}

// DenyDeviceLogin refuses a waiting CLI.
func (s *Server) DenyDeviceLogin(ctx context.Context, req DenyDeviceLoginRequestObject) (DenyDeviceLoginResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	code, err := s.owners.Identity.DenyDeviceLogin(ctx, p, req.UserCode)
	if err != nil {
		return nil, err
	}
	return DenyDeviceLogin200JSONResponse(deviceCodeOut(code)), nil
}

// ListWorkspaces pages through the workspaces the caller may act on.
func (s *Server) ListWorkspaces(ctx context.Context, req ListWorkspacesRequestObject) (ListWorkspacesResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	page := identity.WorkspacePage{Limit: 100}
	if req.Params.Limit != nil {
		page.Limit = *req.Params.Limit
	}
	if req.Params.Cursor != nil {
		page.After = *req.Params.Cursor
	}
	list, err := s.owners.Identity.ListWorkspaces(ctx, p, page)
	if err != nil {
		return nil, err
	}
	out := ListWorkspaces200JSONResponse{Workspaces: workspacesOut(list.Workspaces)}
	if list.Next != "" {
		out.NextCursor = &list.Next
	}
	return out, nil
}

// CreateWorkspace creates a workspace owned by the caller.
func (s *Server) CreateWorkspace(ctx context.Context, req CreateWorkspaceRequestObject) (CreateWorkspaceResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	var connection *uuid.UUID
	if req.Body.Cloud != nil {
		if !p.IsAdmin {
			return nil, identity.ErrAdminRequired
		}
		id, err := s.owners.Compute.WorkspaceConnection(ctx, p.User)
		if err != nil {
			return nil, err
		}
		connection = &id
	}
	ws, err := s.owners.Identity.CreateOwnedWorkspace(ctx, p, req.Body.Name, connection)
	if err != nil {
		return nil, err
	}
	return CreateWorkspace201JSONResponse(workspaceOut(ws)), nil
}

// GetWorkspace returns a workspace in any state.
func (s *Server) GetWorkspace(ctx context.Context, req GetWorkspaceRequestObject) (GetWorkspaceResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	ws, err := s.owners.Identity.GetWorkspace(ctx, p, req.Workspace)
	if err != nil {
		return nil, err
	}
	return GetWorkspace200JSONResponse(workspaceOut(ws)), nil
}

// RenameWorkspace renames a workspace.
func (s *Server) RenameWorkspace(ctx context.Context, req RenameWorkspaceRequestObject) (RenameWorkspaceResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	ws, err := s.owners.Identity.RenameWorkspace(ctx, p, req.Workspace, req.Body.Name)
	if err != nil {
		return nil, err
	}
	return RenameWorkspace200JSONResponse(workspaceOut(ws)), nil
}

// DeleteWorkspace begins or resumes deleting a workspace.
func (s *Server) DeleteWorkspace(ctx context.Context, req DeleteWorkspaceRequestObject) (DeleteWorkspaceResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	ws, err := s.owners.Identity.DeleteWorkspace(ctx, p, req.Workspace)
	if err != nil {
		return nil, err
	}
	return DeleteWorkspace202JSONResponse(workspaceOut(ws)), nil
}

// ListMembers lists a workspace's members.
func (s *Server) ListMembers(ctx context.Context, req ListMembersRequestObject) (ListMembersResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	members, err := s.owners.Identity.ListMembers(ctx, p, req.Workspace)
	if err != nil {
		return nil, err
	}
	out := ListMembers200JSONResponse{Members: make([]apitypes.Member, len(members))}
	for n, m := range members {
		out.Members[n] = memberOut(m)
	}
	return out, nil
}

// SetMemberRole changes a member's role.
func (s *Server) SetMemberRole(ctx context.Context, req SetMemberRoleRequestObject) (SetMemberRoleResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	m, err := s.owners.Identity.SetMemberRole(ctx, p, req.Workspace, identity.UserID(req.User), identity.Role(req.Body.Role))
	if err != nil {
		return nil, err
	}
	return SetMemberRole200JSONResponse(memberOut(m)), nil
}

// RemoveMember removes a member or lets the caller leave.
func (s *Server) RemoveMember(ctx context.Context, req RemoveMemberRequestObject) (RemoveMemberResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Identity.RemoveMember(ctx, p, req.Workspace, identity.UserID(req.User)); err != nil {
		return nil, err
	}
	return RemoveMember204Response{}, nil
}

// ListInvitations lists open invitations.
func (s *Server) ListInvitations(ctx context.Context, req ListInvitationsRequestObject) (ListInvitationsResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	invitations, err := s.owners.Identity.ListInvitations(ctx, p, req.Workspace)
	if err != nil {
		return nil, err
	}
	out := ListInvitations200JSONResponse{Invitations: make([]apitypes.Invitation, len(invitations))}
	for n, inv := range invitations {
		out.Invitations[n] = invitationOut(inv)
	}
	return out, nil
}

// CreateInvitation invites an email address.
func (s *Server) CreateInvitation(ctx context.Context, req CreateInvitationRequestObject) (CreateInvitationResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	role := identity.RoleMember
	if req.Body.Role != nil {
		role = identity.Role(*req.Body.Role)
	}
	inv, err := s.owners.Identity.Invite(ctx, p, req.Workspace, req.Body.Email, role)
	if err != nil {
		return nil, err
	}
	return CreateInvitation201JSONResponse(invitationOut(inv)), nil
}

// RevokeInvitation withdraws an invitation.
func (s *Server) RevokeInvitation(ctx context.Context, req RevokeInvitationRequestObject) (RevokeInvitationResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Identity.RevokeInvitation(ctx, p, req.Workspace, identity.InvitationID(req.Invitation)); err != nil {
		return nil, err
	}
	return RevokeInvitation204Response{}, nil
}

// ResendInvitation sends an invitation again on a new link.
func (s *Server) ResendInvitation(ctx context.Context, req ResendInvitationRequestObject) (ResendInvitationResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	inv, err := s.owners.Identity.ResendInvitation(ctx, p, req.Workspace, identity.InvitationID(req.Invitation))
	if err != nil {
		return nil, err
	}
	return ResendInvitation200JSONResponse(invitationOut(inv)), nil
}

// PreviewInvitation shows what a link offers to a signed-in person.
func (s *Server) PreviewInvitation(ctx context.Context, req PreviewInvitationRequestObject) (PreviewInvitationResponseObject, error) {
	if _, err := s.principal(ctx); err != nil {
		return nil, err
	}
	preview, err := s.owners.Identity.PreviewInvitation(ctx, req.Token)
	if err != nil {
		return nil, err
	}
	return PreviewInvitation200JSONResponse{
		WorkspaceId: uuid.UUID(preview.Workspace), WorkspaceName: preview.WorkspaceName, Email: preview.Email,
		Role: apitypes.InvitationRole(preview.Role), InvitedByName: preview.InvitedByName,
		Expired: preview.Expired, ExpiresAt: preview.ExpiresAt,
	}, nil
}

// AcceptInvitation joins the workspace as the caller.
func (s *Server) AcceptInvitation(ctx context.Context, req AcceptInvitationRequestObject) (AcceptInvitationResponseObject, error) {
	p, err := s.principal(ctx)
	if err != nil {
		return nil, err
	}
	ws, member, err := s.owners.Identity.AcceptInvitation(ctx, p, req.Token)
	if err != nil {
		return nil, err
	}
	return AcceptInvitation201JSONResponse{Workspace: workspaceOut(ws), Member: memberOut(member)}, nil
}

// DeclineInvitation refuses an invitation.
func (s *Server) DeclineInvitation(ctx context.Context, req DeclineInvitationRequestObject) (DeclineInvitationResponseObject, error) {
	if _, err := s.principal(ctx); err != nil {
		return nil, err
	}
	if err := s.owners.Identity.DeclineInvitation(ctx, req.Token); err != nil {
		return nil, err
	}
	return DeclineInvitation204Response{}, nil
}

func userOut(u identity.User) apitypes.User {
	return apitypes.User{
		Id: uuid.UUID(u.ID), Email: u.Email, DisplayName: u.DisplayName, AvatarUrl: u.AvatarURL,
		GithubLogin: u.GitHubLogin, IsAdmin: u.IsAdmin, Status: apitypes.UserStatus(u.Status), CreatedAt: u.CreatedAt,
	}
}

func workspaceOut(ws identity.Workspace) apitypes.Workspace {
	out := apitypes.Workspace{
		Id: uuid.UUID(ws.ID), Name: ws.Name, State: apitypes.WorkspaceState(ws.State), CreatedAt: ws.CreatedAt,
	}
	if ws.Role != "" {
		role := apitypes.WorkspaceRole(ws.Role)
		out.Role = &role
	}
	return out
}

func workspacesOut(list []identity.Workspace) []apitypes.Workspace {
	out := make([]apitypes.Workspace, len(list))
	for n, ws := range list {
		out[n] = workspaceOut(ws)
	}
	return out
}

func tokenOut(t identity.Token, now time.Time) apitypes.Token {
	status := apitypes.TokenStatusActive
	if t.ExpiresAt != nil && !t.ExpiresAt.After(now) {
		status = apitypes.TokenStatusExpired
	}
	return apitypes.Token{
		Id: uuid.UUID(t.ID), Name: t.Name, Prefix: t.Prefix, Device: t.Device, WorkspaceId: (*uuid.UUID)(t.Workspace),
		Status: status, CreatedAt: t.CreatedAt, ExpiresAt: t.ExpiresAt, LastUsedAt: t.LastUsedAt,
	}
}

func deviceCodeOut(c identity.DeviceCode) apitypes.DeviceCode {
	return apitypes.DeviceCode{
		UserCode: c.UserCode, ClientName: c.ClientName, Status: apitypes.DeviceCodeStatus(c.Status),
		CreatedAt: c.CreatedAt, ExpiresAt: c.ExpiresAt,
	}
}

func memberOut(m identity.Member) apitypes.Member {
	return apitypes.Member{
		UserId: uuid.UUID(m.User), DisplayName: m.DisplayName, Email: m.Email,
		Role: apitypes.WorkspaceRole(m.Role), CreatedAt: m.CreatedAt,
	}
}

func invitationOut(inv identity.Invitation) apitypes.Invitation {
	return apitypes.Invitation{
		Id: uuid.UUID(inv.ID), WorkspaceId: uuid.UUID(inv.Workspace), Email: inv.Email,
		Role: apitypes.InvitationRole(inv.Role), InvitedByUserId: (*uuid.UUID)(inv.InvitedBy),
		InvitedByName: inv.InvitedByName, Expired: inv.Expired, Delivery: apitypes.DeliveryState(inv.Delivery),
		ExpiresAt: inv.ExpiresAt, CreatedAt: inv.CreatedAt, UpdatedAt: inv.UpdatedAt,
	}
}
