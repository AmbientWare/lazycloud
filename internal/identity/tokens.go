package identity

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
)

// Token expiry bounds for account tokens; no expiry means it lasts until
// revoked.
const (
	MinTokenLifetime = 24 * time.Hour
	MaxTokenLifetime = 90 * 24 * time.Hour
)

// Token is an API token as its owner sees it. The secret is never stored.
type Token struct {
	ID   TokenID
	Name string
	// Prefix is the token's first characters, empty for tokens issued
	// before prefixes were kept.
	Prefix string
	// Workspace is set for tokens restricted to one workspace.
	Workspace *WorkspaceID
	// Device marks tokens minted by device-code login.
	Device     bool
	CreatedAt  time.Time
	ExpiresAt  *time.Time
	LastUsedAt *time.Time
}

// TokenPage is one page of tokens, newest first. Next is the cursor of the
// following page, nil after the last.
type TokenPage struct {
	Tokens []Token
	Next   *TokenID
}

// CreateAccountToken mints a token that reaches every workspace the account
// belongs to. lifetime nil means no expiry. The secret is returned once.
func (i *Identity) CreateAccountToken(ctx context.Context, p Principal, name string, lifetime *time.Duration) (string, Token, error) {
	if err := p.requireAccount("create tokens"); err != nil {
		return "", Token{}, err
	}
	var expires *time.Time
	if lifetime != nil {
		if *lifetime < MinTokenLifetime || *lifetime > MaxTokenLifetime {
			return "", Token{}, &InvalidError{Message: "a token expires after 1 to 90 days, or never"}
		}
		at := time.Now().Add(*lifetime)
		expires = &at
	}
	return mintToken(ctx, i.queries, p.User, name, expires, false)
}

func mintToken(ctx context.Context, q *Queries, user UserID, name string, expires *time.Time, device bool) (string, Token, error) {
	secret, digest, err := NewToken()
	if err != nil {
		return "", Token{}, err
	}
	row, err := q.InsertToken(ctx, InsertTokenParams{
		UserID: uuid.UUID(user), Name: name, TokenHash: digest, Prefix: DisplayPrefix(secret), ExpiresAt: expires, Device: device,
	})
	if err != nil {
		return "", Token{}, fmt.Errorf("insert token: %w", err)
	}
	return secret, Token{
		ID: TokenID(row.ID), Name: row.Name, Prefix: row.Prefix, Device: row.Device, CreatedAt: row.CreatedAt, ExpiresAt: row.ExpiresAt,
	}, nil
}

// ListTokens pages through the caller's unrevoked tokens, newest first.
// Expired tokens are listed until revoked so their owner sees them lapse.
func (i *Identity) ListTokens(ctx context.Context, p Principal, includeDevice bool, before *TokenID, limit int) (TokenPage, error) {
	if err := p.requireAccount("list tokens"); err != nil {
		return TokenPage{}, err
	}
	rows, err := i.queries.ListUserTokens(ctx, ListUserTokensParams{
		UserID: uuid.UUID(p.User), IncludeDevice: includeDevice, Before: (*uuid.UUID)(before),
		RowLimit: int32(limit + 1), //nolint:gosec // The API bounds limit.
	})
	if err != nil {
		return TokenPage{}, fmt.Errorf("list tokens: %w", err)
	}
	var page TokenPage
	for n, row := range rows {
		if n == limit {
			last := page.Tokens[n-1].ID
			page.Next = &last
			break
		}
		page.Tokens = append(page.Tokens, Token{
			ID: TokenID(row.ID), Name: row.Name, Prefix: row.Prefix, Workspace: (*WorkspaceID)(row.WorkspaceID), Device: row.Device,
			CreatedAt: row.CreatedAt, ExpiresAt: row.ExpiresAt, LastUsedAt: row.LastUsedAt,
		})
	}
	return page, nil
}

// RevokeToken revokes one of the caller's tokens. The token making the
// request cannot revoke itself; signing out ends a session.
func (i *Identity) RevokeToken(ctx context.Context, p Principal, id TokenID) error {
	if err := p.requireAccount("revoke tokens"); err != nil {
		return err
	}
	if p.Token != nil && *p.Token == id {
		return &ConflictError{Message: "a token cannot revoke itself"}
	}
	revoked, err := i.queries.RevokeUserToken(ctx, RevokeUserTokenParams{ID: uuid.UUID(id), UserID: uuid.UUID(p.User)})
	if err != nil {
		return fmt.Errorf("revoke token: %w", err)
	}
	if revoked == 0 {
		return ErrNotFound
	}
	return nil
}

// TokenUseInterval is how often RunTokenUse writes last-used times.
const TokenUseInterval = 30 * time.Second

func (i *Identity) recordTokenUse(id TokenID, at time.Time) {
	i.usageMu.Lock()
	i.usage[id] = at
	i.usageMu.Unlock()
}

// FlushTokenUse writes the recorded last-used times in one statement. On
// failure the times are kept for the next flush unless newer ones replaced
// them.
func (i *Identity) FlushTokenUse(ctx context.Context) error {
	i.usageMu.Lock()
	pending := i.usage
	i.usage = map[TokenID]time.Time{}
	i.usageMu.Unlock()
	if len(pending) == 0 {
		return nil
	}
	ids := make([]uuid.UUID, 0, len(pending))
	times := make([]time.Time, 0, len(pending))
	for id, at := range pending {
		ids = append(ids, uuid.UUID(id))
		times = append(times, at)
	}
	if err := i.queries.TouchTokens(ctx, TouchTokensParams{Ids: ids, UsedAt: times}); err != nil {
		i.usageMu.Lock()
		for id, at := range pending {
			if _, newer := i.usage[id]; !newer {
				i.usage[id] = at
			}
		}
		i.usageMu.Unlock()
		return fmt.Errorf("write token use: %w", err)
	}
	return nil
}

// RunTokenUse flushes last-used times every TokenUseInterval until ctx
// ends, then once more.
func (i *Identity) RunTokenUse(ctx context.Context, logger *slog.Logger) error {
	ticker := time.NewTicker(TokenUseInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			flushCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
			defer cancel()
			if err := i.FlushTokenUse(flushCtx); err != nil {
				logger.WarnContext(flushCtx, "final token use flush", "error", err)
			}
			return nil
		case <-ticker.C:
			if err := i.FlushTokenUse(ctx); err != nil {
				logger.WarnContext(ctx, "token use flush", "error", err)
			}
		}
	}
}
