package identity

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/notifications"
)

// ChannelWorkspace wakes workspace deletion; the payload is the workspace
// id.
const ChannelWorkspace database.Channel = "lc_workspace"

// WorkspacePage asks for workspaces by name after After.
type WorkspacePage struct {
	// MembersOnly lists memberships even for administrators, who otherwise
	// see every workspace.
	MembersOnly bool
	After       string
	Limit       int
}

// WorkspaceList is one page of workspaces; Next is the name to pass as
// After for the following page, empty after the last.
type WorkspaceList struct {
	Workspaces []Workspace
	Next       string
}

// ListWorkspaces pages through the workspaces p may act on: every workspace
// for an administrator, otherwise its memberships, and only the token's
// workspace for a restricted token. Deleting workspaces are included.
func (i *Identity) ListWorkspaces(ctx context.Context, p Principal, page WorkspacePage) (WorkspaceList, error) {
	var after *string
	if page.After != "" {
		after = &page.After
	}
	rows, err := i.queries.ListWorkspaces(ctx, ListWorkspacesParams{
		UserID: uuid.UUID(p.User), MembersOnly: page.MembersOnly || !p.IsAdmin, AfterName: after,
		RestrictTo: (*uuid.UUID)(p.TokenWorkspace), RowLimit: int32(page.Limit + 1), //nolint:gosec // The API bounds limit.
	})
	if err != nil {
		return WorkspaceList{}, fmt.Errorf("list workspaces: %w", err)
	}
	var out WorkspaceList
	for n, row := range rows {
		if n == page.Limit {
			out.Next = out.Workspaces[n-1].Name
			break
		}
		out.Workspaces = append(out.Workspaces, Workspace{
			ID: WorkspaceID(row.ID), Name: row.Name, State: WorkspaceState(row.State),
			Role: Role(row.Role), CreatedAt: row.CreatedAt,
		})
	}
	return out, nil
}

// CreateOwnedWorkspace creates a workspace owned by the caller. Only
// platform administrators create workspaces, with an account credential.
func (i *Identity) CreateOwnedWorkspace(ctx context.Context, p Principal, name string) (Workspace, error) {
	if !p.IsAdmin {
		return Workspace{}, ErrAdminRequired
	}
	if err := p.requireAccount("create workspaces"); err != nil {
		return Workspace{}, err
	}
	var ws Workspace
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		var err error
		ws, err = addWorkspace(ctx, tx, name, p.User)
		return err
	})
	if err != nil {
		return Workspace{}, fmt.Errorf("create workspace: %w", err)
	}
	return ws, nil
}

// RenameWorkspace renames a workspace the caller may act in. Any member may
// rename.
func (i *Identity) RenameWorkspace(ctx context.Context, p Principal, name, newName string) (Workspace, error) {
	ws, err := i.AuthorizeWorkspace(ctx, p, name)
	if err != nil {
		return Workspace{}, err
	}
	if newName == ws.Name {
		return ws, nil
	}
	row, err := i.queries.RenameWorkspace(ctx, RenameWorkspaceParams{Name: newName, ID: uuid.UUID(ws.ID)})
	if isUniqueViolation(err) {
		return Workspace{}, &ConflictError{Message: fmt.Sprintf("workspace name %s is already in use", newName)}
	}
	if errors.Is(err, pgx.ErrNoRows) {
		return Workspace{}, &ConflictError{Message: fmt.Sprintf("workspace %s is being deleted", ws.Name)}
	}
	if err != nil {
		return Workspace{}, fmt.Errorf("rename workspace: %w", err)
	}
	ws.Name, ws.State = row.Name, WorkspaceState(row.State)
	return ws, nil
}

// DeleteWorkspace begins deleting a workspace, or resumes a deletion that
// has not finished. In one transaction the workspace becomes deleting, its
// restricted tokens are revoked and its invitations and their unsent
// emails are withdrawn; from then on every request to it is refused. The
// scheduler finishes the deletion. Only platform administrators delete
// workspaces, and the last active workspace stays.
func (i *Identity) DeleteWorkspace(ctx context.Context, p Principal, name string) (Workspace, error) {
	if !p.IsAdmin {
		return Workspace{}, ErrAdminRequired
	}
	if err := p.requireAccount("delete workspaces"); err != nil {
		return Workspace{}, err
	}
	var ws Workspace
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		if err := q.LockWorkspacesForDeletion(ctx); err != nil {
			return fmt.Errorf("lock workspace deletion: %w", err)
		}
		row, err := q.LockWorkspaceByName(ctx, name)
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock workspace: %w", err)
		}
		ws = Workspace{ID: WorkspaceID(row.ID), Name: row.Name, State: WorkspaceState(row.State), CreatedAt: row.CreatedAt}
		if ws.State == WorkspaceDeleting {
			// Resuming: wake the scheduler, which retries on its own too.
			return database.Notify(ctx, tx, ChannelWorkspace, ws.ID.String())
		}
		others, err := q.CountOtherActiveWorkspaces(ctx, row.ID)
		if err != nil {
			return fmt.Errorf("count active workspaces: %w", err)
		}
		if others == 0 {
			return &ConflictError{Message: "the last workspace cannot be deleted"}
		}
		if _, err := q.MarkWorkspaceDeleting(ctx, row.ID); err != nil {
			return fmt.Errorf("mark workspace deleting: %w", err)
		}
		ws.State = WorkspaceDeleting
		if err := q.RevokeWorkspaceTokens(ctx, &row.ID); err != nil {
			return fmt.Errorf("revoke workspace tokens: %w", err)
		}
		messages, err := q.DeleteWorkspaceInvitations(ctx, row.ID)
		if err != nil {
			return fmt.Errorf("delete invitations: %w", err)
		}
		if err := notifications.Discard(ctx, tx, messageIDs(messages)); err != nil {
			return err
		}
		return database.Notify(ctx, tx, ChannelWorkspace, ws.ID.String())
	})
	if err != nil {
		return Workspace{}, fmt.Errorf("delete workspace %s: %w", name, err)
	}
	return ws, nil
}

func messageIDs(ids []*uuid.UUID) []notifications.MessageID {
	out := make([]notifications.MessageID, 0, len(ids))
	for _, id := range ids {
		if id != nil {
			out = append(out, notifications.MessageID(*id))
		}
	}
	return out
}

// GetWorkspace returns a workspace p may reach, in any state, so a deleting
// workspace can be shown and its deletion resumed.
func (i *Identity) GetWorkspace(ctx context.Context, p Principal, name string) (Workspace, error) {
	return i.reachWorkspace(ctx, p, name)
}

// DeletingWorkspace is a workspace whose deletion has not finished.
type DeletingWorkspace struct {
	ID          WorkspaceID
	Name        string
	RequestedAt time.Time
}

// DeletingWorkspaces returns up to limit workspaces being deleted, oldest
// request first.
func (i *Identity) DeletingWorkspaces(ctx context.Context, limit int) ([]DeletingWorkspace, error) {
	rows, err := i.queries.DeletingWorkspaces(ctx, int32(limit)) //nolint:gosec // Small constant.
	if err != nil {
		return nil, fmt.Errorf("list deleting workspaces: %w", err)
	}
	out := make([]DeletingWorkspace, len(rows))
	for n, row := range rows {
		out[n] = DeletingWorkspace{ID: WorkspaceID(row.ID), Name: row.Name, RequestedAt: row.DeletionRequestedAt}
	}
	return out, nil
}

// FinishWorkspaceDeletion removes a deleting workspace and, by cascade,
// every row it owns, and reports whether it did. The caller first stops its
// containers and deletes its objects. A container that became live since
// then keeps the workspace for a later pass, so no running container loses
// its row. Finishing twice is harmless.
func (i *Identity) FinishWorkspaceDeletion(ctx context.Context, id WorkspaceID) (bool, error) {
	removed := false
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		if _, err := q.LockDeletingWorkspace(ctx, uuid.UUID(id)); errors.Is(err, pgx.ErrNoRows) {
			return nil
		} else if err != nil {
			return fmt.Errorf("lock workspace: %w", err)
		}
		deleted, err := q.DeleteDeletingWorkspace(ctx, uuid.UUID(id))
		if err != nil {
			return fmt.Errorf("delete workspace: %w", err)
		}
		removed = deleted > 0
		return nil
	})
	if err != nil {
		return false, fmt.Errorf("remove workspace %s: %w", id, err)
	}
	return removed, nil
}
