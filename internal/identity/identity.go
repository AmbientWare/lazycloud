// Package identity owns users, workspace membership, API tokens and the
// authorization policy every entry point applies.
package identity

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

var (
	// ErrUnauthenticated means the credential is missing, unknown, expired
	// or revoked.
	ErrUnauthenticated = errors.New("unauthenticated")
	// ErrForbidden means the principal may not reach the workspace. A
	// non-administrator gets it for workspaces that do not exist too, so
	// names of other tenants' workspaces stay private.
	ErrForbidden = errors.New("forbidden")
	// ErrNotFound means the named user or workspace does not exist.
	ErrNotFound = errors.New("not found")
	// ErrExists means a user or workspace with that name already exists.
	ErrExists = errors.New("already exists")
)

// UserID identifies a user.
type UserID uuid.UUID

func (id UserID) String() string { return uuid.UUID(id).String() }

// WorkspaceID identifies a workspace.
type WorkspaceID uuid.UUID

func (id WorkspaceID) String() string { return uuid.UUID(id).String() }

// Principal is the authenticated caller of a public API request.
type Principal struct {
	User    UserID
	Email   string
	IsAdmin bool
	// TokenWorkspace is set when the token reaches only that workspace.
	TokenWorkspace *WorkspaceID
}

// Workspace is an authorized workspace.
type Workspace struct {
	ID   WorkspaceID
	Name string
}

// Identity is the identity owner.
type Identity struct {
	pool    *pgxpool.Pool
	queries *Queries
}

// NewIdentity returns the identity owner over pool.
func NewIdentity(pool *pgxpool.Pool) *Identity {
	return &Identity{pool: pool, queries: New(pool)}
}

// tokenPrefix marks LazyCloud credentials so scanners can recognize them.
const tokenPrefix = "lc_"

// NewToken returns a fresh credential and the digest to store. Tokens carry
// 32 random bytes, so a plain SHA-256 is a sufficient stored form.
func NewToken() (token string, digest []byte, err error) {
	var secret [32]byte
	if _, err := rand.Read(secret[:]); err != nil {
		return "", nil, fmt.Errorf("generate token: %w", err)
	}
	token = tokenPrefix + base64.RawURLEncoding.EncodeToString(secret[:])
	return token, HashToken(token), nil
}

// HashToken is the stored digest of token.
func HashToken(token string) []byte {
	sum := sha256.Sum256([]byte(token))
	return sum[:]
}

// Authenticate resolves an API token to its principal.
func (i *Identity) Authenticate(ctx context.Context, token string) (Principal, error) {
	row, err := i.queries.AuthenticateToken(ctx, HashToken(token))
	if errors.Is(err, pgx.ErrNoRows) {
		return Principal{}, ErrUnauthenticated
	}
	if err != nil {
		return Principal{}, fmt.Errorf("authenticate token: %w", err)
	}
	p := Principal{User: UserID(row.UserID), Email: row.Email, IsAdmin: row.IsAdmin}
	if row.WorkspaceID != nil {
		ws := WorkspaceID(*row.WorkspaceID)
		p.TokenWorkspace = &ws
	}
	return p, nil
}

// AuthorizeWorkspace returns the named workspace when p may act in it: the
// token must reach it, and the user must be a member or an administrator.
func (i *Identity) AuthorizeWorkspace(ctx context.Context, p Principal, name string) (Workspace, error) {
	row, err := i.queries.WorkspaceAccess(ctx, WorkspaceAccessParams{UserID: uuid.UUID(p.User), Name: name})
	if errors.Is(err, pgx.ErrNoRows) {
		if p.IsAdmin && p.TokenWorkspace == nil {
			return Workspace{}, ErrNotFound
		}
		return Workspace{}, ErrForbidden
	}
	if err != nil {
		return Workspace{}, fmt.Errorf("read workspace access: %w", err)
	}
	if p.TokenWorkspace != nil && uuid.UUID(*p.TokenWorkspace) != row.ID {
		return Workspace{}, ErrForbidden
	}
	if !row.Member && !p.IsAdmin {
		return Workspace{}, ErrForbidden
	}
	return Workspace{ID: WorkspaceID(row.ID), Name: row.Name}, nil
}

// Workspaces lists the workspaces p is a member of and its token reaches.
func (i *Identity) Workspaces(ctx context.Context, p Principal) ([]Workspace, error) {
	rows, err := i.queries.MemberWorkspaces(ctx, MemberWorkspacesParams{
		UserID: uuid.UUID(p.User), RestrictTo: (*uuid.UUID)(p.TokenWorkspace),
	})
	if err != nil {
		return nil, fmt.Errorf("list workspaces: %w", err)
	}
	out := make([]Workspace, len(rows))
	for n, row := range rows {
		out[n] = Workspace{ID: WorkspaceID(row.ID), Name: row.Name}
	}
	return out, nil
}

// CreateUser adds a user. Administrators reach every workspace.
func (i *Identity) CreateUser(ctx context.Context, email string, admin bool) (UserID, error) {
	id, err := i.queries.InsertUser(ctx, InsertUserParams{Email: email, IsAdmin: admin})
	if isUniqueViolation(err) {
		return UserID{}, fmt.Errorf("user %s: %w", email, ErrExists)
	}
	if err != nil {
		return UserID{}, fmt.Errorf("insert user: %w", err)
	}
	return UserID(id), nil
}

// CreateWorkspace adds a workspace owned by the user with ownerEmail.
func (i *Identity) CreateWorkspace(ctx context.Context, name, ownerEmail string) (Workspace, error) {
	var ws Workspace
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		owner, err := q.UserByEmail(ctx, ownerEmail)
		if errors.Is(err, pgx.ErrNoRows) {
			return fmt.Errorf("user %s: %w", ownerEmail, ErrNotFound)
		}
		if err != nil {
			return fmt.Errorf("read owner: %w", err)
		}
		id, err := q.InsertWorkspace(ctx, name)
		if isUniqueViolation(err) {
			return fmt.Errorf("workspace %s: %w", name, ErrExists)
		}
		if err != nil {
			return fmt.Errorf("insert workspace: %w", err)
		}
		if err := q.InsertMember(ctx, InsertMemberParams{WorkspaceID: id, UserID: owner.ID, Role: "owner"}); err != nil {
			return fmt.Errorf("insert owner membership: %w", err)
		}
		ws = Workspace{ID: WorkspaceID(id), Name: name}
		return nil
	})
	return ws, err
}

// CreateToken issues an API token for the user with email, restricted to
// workspace when it is not empty. The token is returned once and only its
// digest is stored.
func (i *Identity) CreateToken(ctx context.Context, email, workspace, name string) (string, error) {
	user, err := i.queries.UserByEmail(ctx, email)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", fmt.Errorf("user %s: %w", email, ErrNotFound)
	}
	if err != nil {
		return "", fmt.Errorf("read user: %w", err)
	}
	var restrict *uuid.UUID
	if workspace != "" {
		ws, err := i.queries.WorkspaceByName(ctx, workspace)
		if errors.Is(err, pgx.ErrNoRows) {
			return "", fmt.Errorf("workspace %s: %w", workspace, ErrNotFound)
		}
		if err != nil {
			return "", fmt.Errorf("read workspace: %w", err)
		}
		restrict = &ws.ID
	}
	token, digest, err := NewToken()
	if err != nil {
		return "", err
	}
	if _, err := i.queries.InsertToken(ctx, InsertTokenParams{
		UserID: user.ID, WorkspaceID: restrict, Name: name, TokenHash: digest,
	}); err != nil {
		return "", fmt.Errorf("insert token: %w", err)
	}
	return token, nil
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}
