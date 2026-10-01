package identity

import (
	"context"
	"fmt"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// PlatformRole is an account's standing on the platform.
type PlatformRole string

const (
	// PlatformAdministrator reaches every workspace and manages accounts.
	PlatformAdministrator PlatformRole = "administrator"
	// PlatformMember reaches the workspaces it is a member of.
	PlatformMember PlatformRole = "member"
)

func (r PlatformRole) isAdmin() (bool, error) {
	switch r {
	case PlatformAdministrator:
		return true, nil
	case PlatformMember:
		return false, nil
	}
	return false, &InvalidError{Message: fmt.Sprintf("unknown platform role %q", r)}
}

// UserStatus says whether an account may sign in.
type UserStatus string

const (
	// UserActive signs in and authenticates.
	UserActive UserStatus = "active"
	// UserDisabled cannot sign in, and its credentials were ended when it
	// was disabled. Running work is not stopped.
	UserDisabled UserStatus = "disabled"
)

func (s UserStatus) check() error {
	switch s {
	case UserActive, UserDisabled:
		return nil
	}
	return &InvalidError{Message: fmt.Sprintf("unknown account status %q", s)}
}

// Account listing bounds.
const (
	MaxUserPage   = 200
	MaxUserSearch = 200
)

// UserQuery asks for one page of accounts. Nil Role and Status match every
// account; an empty Search matches every account.
type UserQuery struct {
	// Search matches the display name, email or GitHub login, ignoring case.
	Search string
	Role   *PlatformRole
	Status *UserStatus
	// After is the Next of the previous page.
	After *UserID
	Limit int
}

// UserPage is one page of accounts, oldest first. Next is the cursor of the
// following page, nil after the last.
type UserPage struct {
	Users []User
	Next  *UserID
}

// ListUsers pages through every account, including ones that never signed
// in. Only a platform administrator with an account credential may list
// them. Billing lists accounts through it and adds its own columns for the
// returned ids.
func (i *Identity) ListUsers(ctx context.Context, p Principal, q UserQuery) (UserPage, error) {
	if err := requireAccountAdmin(p); err != nil {
		return UserPage{}, err
	}
	if q.Limit < 1 || q.Limit > MaxUserPage {
		return UserPage{}, &InvalidError{Message: fmt.Sprintf("limit must be between 1 and %d", MaxUserPage)}
	}
	params := ListUsersParams{AfterID: (*uuid.UUID)(q.After), RowLimit: int32(q.Limit + 1)} //nolint:gosec // Bounded above.
	if term := strings.TrimSpace(q.Search); term != "" {
		if len([]rune(term)) > MaxUserSearch {
			return UserPage{}, &InvalidError{Message: fmt.Sprintf("search is at most %d characters", MaxUserSearch)}
		}
		pattern := "%" + escapeLike(term) + "%"
		params.Pattern = &pattern
	}
	if q.Role != nil {
		admin, err := q.Role.isAdmin()
		if err != nil {
			return UserPage{}, err
		}
		params.IsAdmin = &admin
	}
	if q.Status != nil {
		if err := q.Status.check(); err != nil {
			return UserPage{}, err
		}
		status := string(*q.Status)
		params.Status = &status
	}
	rows, err := i.queries.ListUsers(ctx, params)
	if err != nil {
		return UserPage{}, fmt.Errorf("list users: %w", err)
	}
	var page UserPage
	for n, row := range rows {
		if n == q.Limit {
			last := page.Users[n-1].ID
			page.Next = &last
			break
		}
		page.Users = append(page.Users, userFrom(UserProfileRow(row)))
	}
	return page, nil
}

// SetUserRole grants or withdraws platform administration.
func (i *Identity) SetUserRole(ctx context.Context, p Principal, target UserID, role PlatformRole) (User, error) {
	admin, err := role.isAdmin()
	if err != nil {
		return User{}, err
	}
	return i.administer(ctx, p, target, func(q *Queries) error {
		if err := q.SetUserAdmin(ctx, SetUserAdminParams{IsAdmin: admin, ID: uuid.UUID(target)}); err != nil {
			return fmt.Errorf("set role: %w", err)
		}
		return nil
	})
}

// SetUserStatus enables or disables an account. Disabling ends its
// credentials in the same transaction: its API tokens are revoked, its
// browser sessions deleted and its approved, uncollected device codes
// expired, so enabling it again restores none of them. Its running work is
// not stopped.
func (i *Identity) SetUserStatus(ctx context.Context, p Principal, target UserID, status UserStatus) (User, error) {
	if err := status.check(); err != nil {
		return User{}, err
	}
	return i.administer(ctx, p, target, func(q *Queries) error {
		id := uuid.UUID(target)
		if err := q.SetUserStatus(ctx, SetUserStatusParams{Status: string(status), ID: id}); err != nil {
			return fmt.Errorf("set status: %w", err)
		}
		if status != UserDisabled {
			return nil
		}
		if err := q.RevokeUserTokens(ctx, id); err != nil {
			return fmt.Errorf("revoke tokens: %w", err)
		}
		if err := q.DeleteUserSessions(ctx, id); err != nil {
			return fmt.Errorf("delete sessions: %w", err)
		}
		if err := q.ExpireUserDeviceCodes(ctx, &id); err != nil {
			return fmt.Errorf("expire device codes: %w", err)
		}
		return nil
	})
}

// administer applies change to another account. The caller and the target
// rows are locked, and the change commits only while the caller is still an
// active administrator. Because nobody changes their own account, every
// committed change leaves at least the caller administering the platform:
// two administrators demoting or disabling each other concurrently
// serialize on the locks, and the second is refused.
func (i *Identity) administer(ctx context.Context, p Principal, target UserID, change func(*Queries) error) (User, error) {
	if err := requireAccountAdmin(p); err != nil {
		return User{}, err
	}
	if target == p.User {
		return User{}, &ConflictError{Message: "you cannot change your own role or status; ask another administrator"}
	}
	var user User
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		rows, err := q.LockUsers(ctx, []uuid.UUID{uuid.UUID(p.User), uuid.UUID(target)})
		if err != nil {
			return fmt.Errorf("lock users: %w", err)
		}
		callerAdmin, found := false, false
		for _, row := range rows {
			switch UserID(row.ID) {
			case p.User:
				callerAdmin = row.IsAdmin && UserStatus(row.Status) == UserActive
			case target:
				found = true
			}
		}
		if !callerAdmin {
			return ErrAdminRequired
		}
		if !found {
			return ErrNotFound
		}
		if err := change(q); err != nil {
			return err
		}
		row, err := q.UserProfile(ctx, uuid.UUID(target))
		if err != nil {
			return fmt.Errorf("read user: %w", err)
		}
		user = userFrom(row)
		return nil
	})
	if err != nil {
		return User{}, fmt.Errorf("change account %s: %w", target, err)
	}
	return user, nil
}

func requireAccountAdmin(p Principal) error {
	if !p.IsAdmin {
		return ErrAdminRequired
	}
	return p.requireAccount("manage accounts")
}

// escapeLike quotes LIKE wildcards with the default escape character.
func escapeLike(s string) string {
	var b strings.Builder
	for _, r := range s {
		if strings.ContainsRune(`\%_`, r) {
			b.WriteByte('\\')
		}
		b.WriteRune(r)
	}
	return b.String()
}

func userFrom(row UserProfileRow) User {
	return User{
		ID: UserID(row.ID), Email: deref(row.Email), DisplayName: row.DisplayName, AvatarURL: row.AvatarUrl,
		GitHubLogin: row.GithubLogin, IsAdmin: row.IsAdmin, Status: UserStatus(row.Status), CreatedAt: row.CreatedAt,
	}
}
