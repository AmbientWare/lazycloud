package api

import (
	"context"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/schedules"
	"github.com/AmbientWare/lazycloud/internal/secrets"
)

// defaultPageSize is the page size when a list names no limit.
const defaultPageSize = 100

// valueOr is an optional parameter's value, or def without one.
func valueOr[T any](p *T, def T) T {
	if p == nil {
		return def
	}
	return *p
}

// pageOf trims rows read with a limit of limit+1 and returns the cursor
// after the last row it keeps, nil on the last page.
func pageOf[T any](rows []T, limit int, cursor func(T) any) ([]T, *string) {
	if len(rows) <= limit {
		return rows, nil
	}
	return rows[:limit], encodeCursor(cursor(rows[limit-1]))
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
func (s *server) ListSecrets(ctx context.Context, req ListSecretsRequestObject) (ListSecretsResponseObject, error) {
	ws := workspaceFrom(ctx)
	page, next, err := s.owners.Secrets.List(ctx, ws.ID, valueOr(req.Params.Cursor, ""), valueOr(req.Params.Limit, defaultPageSize))
	if err != nil {
		return nil, err
	}
	out := ListSecrets200JSONResponse{Secrets: make([]apitypes.Secret, len(page)), NextCursor: optional(next)}
	for n, secret := range page {
		out.Secrets[n] = secretOut(secret)
	}
	return out, nil
}

// GetSecret returns a secret's metadata.
func (s *server) GetSecret(ctx context.Context, req GetSecretRequestObject) (GetSecretResponseObject, error) {
	ws := workspaceFrom(ctx)
	secret, err := s.owners.Secrets.Get(ctx, ws.ID, req.Secret)
	if err != nil {
		return nil, err
	}
	return GetSecret200JSONResponse(secretOut(secret)), nil
}

// SetSecret stores a secret's value: If-None-Match: * only creates it and
// If-Match: * only replaces an existing one.
func (s *server) SetSecret(ctx context.Context, req SetSecretRequestObject) (SetSecretResponseObject, error) {
	ws := workspaceFrom(ctx)
	store := s.owners.Secrets.Set
	switch {
	case req.Params.IfNoneMatch != nil:
		store = s.owners.Secrets.Create
	case req.Params.IfMatch != nil:
		store = s.owners.Secrets.Update
	}
	secret, err := store(ctx, ws.ID, req.Secret, req.Body.Value)
	if err != nil {
		return nil, err
	}
	return SetSecret200JSONResponse(secretOut(secret)), nil
}

// DeleteSecret removes a secret.
func (s *server) DeleteSecret(ctx context.Context, req DeleteSecretRequestObject) (DeleteSecretResponseObject, error) {
	ws := workspaceFrom(ctx)
	if err := s.owners.Secrets.Delete(ctx, ws.ID, req.Secret); err != nil {
		return nil, err
	}
	return DeleteSecret204Response{}, nil
}

// GetSecretValue reveals a secret's value to every principal that reaches
// the workspace; tokens carry no narrower scope.
func (s *server) GetSecretValue(ctx context.Context, req GetSecretValueRequestObject) (GetSecretValueResponseObject, error) {
	ws := workspaceFrom(ctx)
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
