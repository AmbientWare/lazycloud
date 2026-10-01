// Package identity owns users, sign-in, browser sessions, API tokens,
// workspaces, membership, invitations and the authorization policy every
// entry point applies.
package identity

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"time"

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
	// ErrNotFound means the named user, workspace or resource does not
	// exist.
	ErrNotFound = errors.New("not found")
	// ErrExists means a user or workspace with that name already exists.
	ErrExists = errors.New("already exists")
	// ErrAdminRequired means the action needs a platform administrator.
	ErrAdminRequired = errors.New("platform administrator access is required")
)

// ConflictError is a request the current state refuses.
type ConflictError struct{ Message string }

func (e *ConflictError) Error() string { return e.Message }

// InvalidError is a request whose content identity rejects.
type InvalidError struct{ Message string }

func (e *InvalidError) Error() string { return e.Message }

// RoleError means the caller's workspace role is below Required.
type RoleError struct{ Required Role }

func (e *RoleError) Error() string {
	return fmt.Sprintf("this needs the %s role in the workspace", e.Required)
}

// AccountError means a token restricted to one workspace asked for
// something only an account credential may do.
type AccountError struct{ Action string }

func (e *AccountError) Error() string {
	return fmt.Sprintf("a token restricted to one workspace cannot %s", e.Action)
}

// UserID identifies a user.
type UserID uuid.UUID

func (id UserID) String() string { return uuid.UUID(id).String() }

// WorkspaceID identifies a workspace.
type WorkspaceID uuid.UUID

func (id WorkspaceID) String() string { return uuid.UUID(id).String() }

// TokenID identifies an API token.
type TokenID uuid.UUID

func (id TokenID) String() string { return uuid.UUID(id).String() }

// SessionID identifies a browser session.
type SessionID uuid.UUID

func (id SessionID) String() string { return uuid.UUID(id).String() }

// Principal is the authenticated caller of a public API request.
type Principal struct {
	User    UserID
	Email   string
	IsAdmin bool
	// TokenWorkspace is set when the token reaches only that workspace.
	TokenWorkspace *WorkspaceID
	// Token is the API token that authenticated the request; Session is
	// the browser session. At most one is set.
	Token   *TokenID
	Session *SessionID
}

// requireAccount refuses a token restricted to one workspace: it carries
// no authority over the account, so it cannot act for it.
func (p Principal) requireAccount(action string) error {
	if p.TokenWorkspace != nil {
		return &AccountError{Action: action}
	}
	return nil
}

// Role is a user's standing in one workspace.
type Role string

const (
	// RoleOwner owns the workspace; there is exactly one.
	RoleOwner Role = "owner"
	// RoleAdministrator invites, changes roles and removes members.
	RoleAdministrator Role = "administrator"
	// RoleMember uses the workspace.
	RoleMember Role = "member"
)

// covers reports whether r carries at least the authority of required.
func (r Role) covers(required Role) bool { return r.rank() >= required.rank() }

func (r Role) rank() int {
	switch r {
	case RoleOwner:
		return 3
	case RoleAdministrator:
		return 2
	case RoleMember:
		return 1
	}
	return 0
}

// WorkspaceState is a workspace's lifecycle state.
type WorkspaceState string

const (
	// WorkspaceActive accepts requests.
	WorkspaceActive WorkspaceState = "active"
	// WorkspaceDeleting refuses requests while the scheduler removes it.
	WorkspaceDeleting WorkspaceState = "deleting"
)

// Workspace is an authorized workspace.
type Workspace struct {
	ID    WorkspaceID
	Name  string
	State WorkspaceState
	// Role is the caller's role; empty for an administrator who is not a
	// member.
	Role      Role
	CreatedAt time.Time
}

// Config is what identity needs beyond the database.
type Config struct {
	// PublicURL is the dashboard origin, such as https://lazycloud.dev.
	// Invitation links and the GitHub callback are under it.
	PublicURL string
	GitHub    GitHubConfig
}

// Identity is the identity owner.
type Identity struct {
	pool    *pgxpool.Pool
	queries *Queries
	cfg     Config
	github  *gitHub

	// usage holds token last-used times not yet written; see FlushTokenUse.
	usageMu sync.Mutex
	usage   map[TokenID]time.Time
}

// NewIdentity returns the identity owner over pool.
func NewIdentity(pool *pgxpool.Pool, cfg Config) *Identity {
	cfg.PublicURL = strings.TrimRight(cfg.PublicURL, "/")
	return &Identity{
		pool: pool, queries: New(pool), cfg: cfg,
		github: newGitHub(cfg.GitHub, cfg.PublicURL+GitHubCallbackPath),
		usage:  map[TokenID]time.Time{},
	}
}

// TokenPrefix marks LazyCloud credentials so scanners, and the edge that
// keeps them from workloads, can recognize them.
const TokenPrefix = "lc_"

// NewToken returns a fresh credential and the digest to store. Tokens carry
// 32 random bytes, so a plain SHA-256 is a sufficient stored form.
func NewToken() (token string, digest []byte, err error) {
	return newSecret(TokenPrefix)
}

func newSecret(prefix string) (secret string, digest []byte, err error) {
	var raw [32]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", nil, fmt.Errorf("generate secret: %w", err)
	}
	secret = prefix + base64.RawURLEncoding.EncodeToString(raw[:])
	return secret, HashToken(secret), nil
}

// HashToken is the stored digest of token.
func HashToken(token string) []byte {
	sum := sha256.Sum256([]byte(token))
	return sum[:]
}

// Authenticate resolves an API token to its principal and records its use.
func (i *Identity) Authenticate(ctx context.Context, token string) (Principal, error) {
	row, err := i.queries.AuthenticateToken(ctx, HashToken(token))
	if errors.Is(err, pgx.ErrNoRows) {
		return Principal{}, ErrUnauthenticated
	}
	if err != nil {
		return Principal{}, fmt.Errorf("authenticate token: %w", err)
	}
	id := TokenID(row.ID)
	i.recordTokenUse(id, time.Now())
	p := Principal{User: UserID(row.UserID), Email: deref(row.Email), IsAdmin: row.IsAdmin, Token: &id}
	if row.WorkspaceID != nil {
		ws := WorkspaceID(*row.WorkspaceID)
		p.TokenWorkspace = &ws
	}
	return p, nil
}

// AuthorizeWorkspace returns the named workspace when p may act in it: the
// token must reach it, the user must be a member or an administrator, and
// the workspace must not be deleting.
func (i *Identity) AuthorizeWorkspace(ctx context.Context, p Principal, name string) (Workspace, error) {
	ws, err := i.reachWorkspace(ctx, p, name)
	if err != nil {
		return Workspace{}, err
	}
	if ws.State == WorkspaceDeleting {
		return Workspace{}, &ConflictError{Message: fmt.Sprintf("workspace %s is being deleted", ws.Name)}
	}
	return ws, nil
}

// AuthorizeWorkspaceRole is AuthorizeWorkspace for actions that need at
// least required. Administrators pass without membership.
func (i *Identity) AuthorizeWorkspaceRole(ctx context.Context, p Principal, name string, required Role) (Workspace, error) {
	ws, err := i.AuthorizeWorkspace(ctx, p, name)
	if err != nil {
		return Workspace{}, err
	}
	if !p.IsAdmin && !ws.Role.covers(required) {
		return Workspace{}, &RoleError{Required: required}
	}
	return ws, nil
}

// reachWorkspace applies the token and membership rules in any state.
func (i *Identity) reachWorkspace(ctx context.Context, p Principal, name string) (Workspace, error) {
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
	if row.Role == "" && !p.IsAdmin {
		return Workspace{}, ErrForbidden
	}
	return Workspace{
		ID: WorkspaceID(row.ID), Name: row.Name, State: WorkspaceState(row.State),
		Role: Role(row.Role), CreatedAt: row.CreatedAt,
	}, nil
}

// maxMemberships bounds the workspaces Workspaces returns.
const maxMemberships = 1000

// Workspaces lists the workspaces p is a member of and its token reaches,
// deleting ones included.
func (i *Identity) Workspaces(ctx context.Context, p Principal) ([]Workspace, error) {
	page, err := i.ListWorkspaces(ctx, p, WorkspacePage{MembersOnly: true, Limit: maxMemberships})
	if err != nil {
		return nil, err
	}
	return page.Workspaces, nil
}

// CreateUser adds a user. Administrators reach every workspace.
func (i *Identity) CreateUser(ctx context.Context, email string, admin bool) (UserID, error) {
	id, err := i.queries.InsertUser(ctx, InsertUserParams{Email: &email, IsAdmin: admin})
	if isUniqueViolation(err) {
		return UserID{}, fmt.Errorf("user %s: %w", email, ErrExists)
	}
	if err != nil {
		return UserID{}, fmt.Errorf("insert user: %w", err)
	}
	return UserID(id), nil
}

// CreateWorkspace adds a workspace owned by the user with ownerEmail. It is
// the bootstrap path of the admin command.
func (i *Identity) CreateWorkspace(ctx context.Context, name, ownerEmail string) (Workspace, error) {
	var ws Workspace
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		owner, err := q.UserByEmail(ctx, &ownerEmail)
		if errors.Is(err, pgx.ErrNoRows) {
			return fmt.Errorf("user %s: %w", ownerEmail, ErrNotFound)
		}
		if err != nil {
			return fmt.Errorf("read owner: %w", err)
		}
		ws, err = addWorkspace(ctx, q, name, UserID(owner.ID))
		return err
	})
	if err != nil {
		return Workspace{}, fmt.Errorf("create workspace: %w", err)
	}
	return ws, nil
}

// addWorkspace adds a workspace and its owner's membership.
func addWorkspace(ctx context.Context, q *Queries, name string, owner UserID) (Workspace, error) {
	if err := admitWorkspace(ctx, q, owner); err != nil {
		return Workspace{}, err
	}
	row, err := q.InsertWorkspace(ctx, name)
	if isUniqueViolation(err) {
		return Workspace{}, fmt.Errorf("workspace %s: %w", name, ErrExists)
	}
	if err != nil {
		return Workspace{}, fmt.Errorf("insert workspace: %w", err)
	}
	if err := q.InsertMember(ctx, InsertMemberParams{WorkspaceID: row.ID, UserID: uuid.UUID(owner), Role: string(RoleOwner)}); err != nil {
		return Workspace{}, fmt.Errorf("insert owner membership: %w", err)
	}
	return Workspace{ID: WorkspaceID(row.ID), Name: name, State: WorkspaceActive, Role: RoleOwner, CreatedAt: row.CreatedAt}, nil
}

// admitWorkspace is where billing will apply plan limits on the workspaces
// an account owns. Workspace creation calls it inside its transaction. No
// limits exist until billing owns them.
func admitWorkspace(_ context.Context, _ *Queries, _ UserID) error { return nil }

// admitMember is where billing will apply plan limits on a workspace's
// members, such as Team's three. Invitations call it when sent and when
// accepted, inside their transactions. No limits exist until billing owns
// them.
func admitMember(_ context.Context, _ *Queries, _ WorkspaceID) error { return nil }

// CreateToken issues an API token for the user with email, restricted to
// workspace when it is not empty. The token is returned once and only its
// digest is stored. It is the bootstrap path of the admin command.
func (i *Identity) CreateToken(ctx context.Context, email, workspace, name string) (string, error) {
	user, err := i.queries.UserByEmail(ctx, &email)
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

// Housekeeping removes expired sessions and device codes. It is bounded per
// call; the scheduler runs it periodically.
func (i *Identity) Housekeeping(ctx context.Context, logger *slog.Logger) error {
	const limit = 1000
	sessions, err := i.queries.PruneSessions(ctx, limit)
	if err != nil {
		return fmt.Errorf("prune sessions: %w", err)
	}
	codes, err := i.queries.PruneDeviceCodes(ctx, limit)
	if err != nil {
		return fmt.Errorf("prune device codes: %w", err)
	}
	if sessions > 0 || codes > 0 {
		logger.InfoContext(ctx, "identity housekeeping", "sessions", sessions, "device_codes", codes)
	}
	return nil
}

func isUniqueViolation(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && pgErr.Code == "23505"
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
