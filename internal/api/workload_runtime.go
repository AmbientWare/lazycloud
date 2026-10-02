package api

import (
	"context"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/schedules"
	"github.com/AmbientWare/lazycloud/internal/secrets"
)

// defaultPageSize is the page size when a list names no limit.
const defaultPageSize = 100

func pageArgs(cursor *string, limit *int) (string, int) {
	c, n := "", defaultPageSize
	if cursor != nil {
		c = *cursor
	}
	if limit != nil {
		n = *limit
	}
	return c, n
}

func secretOut(s secrets.Secret) apitypes.Secret {
	used := make([]apitypes.WorkloadRef, len(s.UsedBy))
	for n, u := range s.UsedBy {
		used[n] = apitypes.WorkloadRef{App: u.App, Kind: apitypes.WorkloadKind(u.Kind), Name: u.Workload}
	}
	return apitypes.Secret{Name: s.Name, CreatedAt: s.CreatedAt, UpdatedAt: s.UpdatedAt, UsedBy: used}
}

func optional(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// ListSecrets returns one page of secret names.
func (s *Server) ListSecrets(ctx context.Context, req ListSecretsRequestObject) (ListSecretsResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	cursor, limit := pageArgs(req.Params.Cursor, req.Params.Limit)
	page, next, err := s.owners.Secrets.List(ctx, ws.ID, cursor, limit)
	if err != nil {
		return nil, err
	}
	out := ListSecrets200JSONResponse{Secrets: make([]apitypes.Secret, len(page)), NextCursor: optional(next)}
	for n, secret := range page {
		out.Secrets[n] = secretOut(secret)
	}
	return out, nil
}

// CreateSecret stores a new secret.
func (s *Server) CreateSecret(ctx context.Context, req CreateSecretRequestObject) (CreateSecretResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	secret, err := s.owners.Secrets.Create(ctx, ws.ID, req.Body.Name, req.Body.Value)
	if err != nil {
		return nil, err
	}
	return CreateSecret201JSONResponse(secretOut(secret)), nil
}

// GetSecret returns a secret's metadata.
func (s *Server) GetSecret(ctx context.Context, req GetSecretRequestObject) (GetSecretResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	secret, err := s.owners.Secrets.Get(ctx, ws.ID, req.Secret)
	if err != nil {
		return nil, err
	}
	return GetSecret200JSONResponse(secretOut(secret)), nil
}

// SetSecret creates the secret or replaces its value.
func (s *Server) SetSecret(ctx context.Context, req SetSecretRequestObject) (SetSecretResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	secret, err := s.owners.Secrets.Set(ctx, ws.ID, req.Secret, req.Body.Value)
	if err != nil {
		return nil, err
	}
	return SetSecret200JSONResponse(secretOut(secret)), nil
}

// UpdateSecret replaces an existing secret's value.
func (s *Server) UpdateSecret(ctx context.Context, req UpdateSecretRequestObject) (UpdateSecretResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	secret, err := s.owners.Secrets.Update(ctx, ws.ID, req.Secret, req.Body.Value)
	if err != nil {
		return nil, err
	}
	return UpdateSecret200JSONResponse(secretOut(secret)), nil
}

// DeleteSecret removes a secret.
func (s *Server) DeleteSecret(ctx context.Context, req DeleteSecretRequestObject) (DeleteSecretResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Secrets.Delete(ctx, ws.ID, req.Secret); err != nil {
		return nil, err
	}
	return DeleteSecret204Response{}, nil
}

// GetSecretValue reveals a secret's value. Workspace tokens carry no scope
// yet, so every principal that reaches the workspace may call it; the
// identity packet's read-only scope must deny it.
func (s *Server) GetSecretValue(ctx context.Context, req GetSecretValueRequestObject) (GetSecretValueResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	secret, value, err := s.owners.Secrets.Reveal(ctx, ws.ID, req.Secret)
	if err != nil {
		return nil, err
	}
	return GetSecretValue200JSONResponse{
		Name: secret.Name, Value: value, CreatedAt: secret.CreatedAt, UpdatedAt: secret.UpdatedAt,
	}, nil
}

func scheduleOut(s schedules.Schedule) apitypes.Schedule {
	return apitypes.Schedule{
		Cron: s.Expression, Timezone: apitypes.UTC, NextRunAt: s.NextRunAt,
		LastRunAt: s.LastRunAt, LastTaskId: s.LastTask, LastError: s.LastError,
	}
}
