package identity

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// Member is one membership of a workspace.
type Member struct {
	User        UserID
	DisplayName string
	Email       string
	Role        Role
	CreatedAt   time.Time
}

// ListMembers lists a workspace's members, owner first. Any member may.
func (i *Identity) ListMembers(ctx context.Context, p Principal, workspace string) ([]Member, error) {
	ws, err := i.AuthorizeWorkspace(ctx, p, workspace)
	if err != nil {
		return nil, err
	}
	rows, err := i.queries.ListMembers(ctx, uuid.UUID(ws.ID))
	if err != nil {
		return nil, fmt.Errorf("list members: %w", err)
	}
	out := make([]Member, len(rows))
	for n, row := range rows {
		out[n] = Member{
			User: UserID(row.UserID), DisplayName: row.DisplayName, Email: deref(row.Email),
			Role: Role(row.Role), CreatedAt: row.CreatedAt,
		}
	}
	return out, nil
}

// SetMemberRole makes a member an administrator or a member. It takes an
// administrator; the owner's role never changes.
func (i *Identity) SetMemberRole(ctx context.Context, p Principal, workspace string, user UserID, role Role) (Member, error) {
	if role != RoleAdministrator && role != RoleMember {
		return Member{}, &InvalidError{Message: "a workspace has exactly one owner; choose administrator or member"}
	}
	ws, err := i.AuthorizeWorkspaceRole(ctx, p, workspace, RoleAdministrator)
	if err != nil {
		return Member{}, err
	}
	var member Member
	err = i.changeMember(ctx, ws.ID, user, func(q *Queries, current LockMemberRow) error {
		if Role(current.Role) == RoleOwner {
			return &ConflictError{Message: "the workspace owner's role cannot be changed"}
		}
		member = Member{
			User: user, DisplayName: current.DisplayName, Email: deref(current.Email), Role: role, CreatedAt: current.CreatedAt,
		}
		if Role(current.Role) == role {
			return nil
		}
		if err := q.SetMemberRole(ctx, SetMemberRoleParams{Role: string(role), WorkspaceID: uuid.UUID(ws.ID), UserID: uuid.UUID(user)}); err != nil {
			return fmt.Errorf("set member role: %w", err)
		}
		return nil
	})
	return member, err
}

// RemoveMember takes user out of the workspace. Removing someone else takes
// an administrator; removing yourself is leaving and takes only membership.
// The owner can do neither.
func (i *Identity) RemoveMember(ctx context.Context, p Principal, workspace string, user UserID) error {
	leaving := user == p.User
	required := RoleAdministrator
	if leaving {
		required = RoleMember
	}
	ws, err := i.AuthorizeWorkspaceRole(ctx, p, workspace, required)
	if err != nil {
		return err
	}
	return i.changeMember(ctx, ws.ID, user, func(q *Queries, current LockMemberRow) error {
		if Role(current.Role) == RoleOwner {
			if leaving {
				return &ConflictError{Message: "the workspace owner cannot leave it"}
			}
			return &ConflictError{Message: "the workspace owner cannot be removed"}
		}
		if err := q.DeleteMember(ctx, DeleteMemberParams{WorkspaceID: uuid.UUID(ws.ID), UserID: uuid.UUID(user)}); err != nil {
			return fmt.Errorf("remove member: %w", err)
		}
		return nil
	})
}

// changeMember runs change on a locked membership of an active workspace.
func (i *Identity) changeMember(ctx context.Context, ws WorkspaceID, user UserID, change func(*Queries, LockMemberRow) error) error {
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		if _, err := q.LockActiveWorkspace(ctx, uuid.UUID(ws)); errors.Is(err, pgx.ErrNoRows) {
			return &ConflictError{Message: "the workspace is being deleted"}
		} else if err != nil {
			return fmt.Errorf("lock workspace: %w", err)
		}
		current, err := q.LockMember(ctx, LockMemberParams{WorkspaceID: uuid.UUID(ws), UserID: uuid.UUID(user)})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock member: %w", err)
		}
		return change(q, current)
	})
	if err != nil {
		return fmt.Errorf("change member %s: %w", user, err)
	}
	return nil
}
