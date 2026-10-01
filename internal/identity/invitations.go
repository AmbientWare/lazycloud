package identity

import (
	"context"
	"errors"
	"fmt"
	"html"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/notifications"
)

// InvitationTTL is how long an invitation link works after it is sent.
const InvitationTTL = 14 * 24 * time.Hour

const (
	displayName      = "LazyCloud"
	invitationPrefix = "lci_"
)

// InvitationID identifies an invitation.
type InvitationID uuid.UUID

func (id InvitationID) String() string { return uuid.UUID(id).String() }

// Invitation is an open offer as the workspace's administrators see it.
type Invitation struct {
	ID            InvitationID
	Workspace     WorkspaceID
	Email         string
	Role          Role
	InvitedBy     *UserID
	InvitedByName string
	// Expired is decided against the database clock, so every reader
	// agrees.
	Expired bool
	// Delivery is what became of the email carrying the current link.
	Delivery  notifications.DeliveryState
	ExpiresAt time.Time
	CreatedAt time.Time
	UpdatedAt time.Time
}

// Invite offers membership with role to email and queues the invitation
// email in the same transaction. It takes a workspace administrator.
func (i *Identity) Invite(ctx context.Context, p Principal, workspace, email string, role Role) (Invitation, error) {
	address, err := normalizeEmail(email)
	if err != nil {
		return Invitation{}, err
	}
	if role != RoleAdministrator && role != RoleMember {
		return Invitation{}, &InvalidError{Message: "an invitation offers administrator or member"}
	}
	ws, err := i.AuthorizeWorkspaceRole(ctx, p, workspace, RoleAdministrator)
	if err != nil {
		return Invitation{}, err
	}
	var inv Invitation
	err = pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		if _, err := q.LockActiveWorkspace(ctx, uuid.UUID(ws.ID)); errors.Is(err, pgx.ErrNoRows) {
			return &ConflictError{Message: "the workspace is being deleted"}
		} else if err != nil {
			return fmt.Errorf("lock workspace: %w", err)
		}
		member, err := q.MemberWithEmail(ctx, MemberWithEmailParams{WorkspaceID: uuid.UUID(ws.ID), Email: address})
		if err != nil {
			return fmt.Errorf("check membership: %w", err)
		}
		if member {
			return &ConflictError{Message: fmt.Sprintf("%s is already a member of this workspace", address)}
		}
		if err := admitMember(ctx, q, ws.ID); err != nil {
			return err
		}
		inviter, err := q.UserProfile(ctx, uuid.UUID(p.User))
		if err != nil {
			return fmt.Errorf("read inviter: %w", err)
		}
		inviterName := inviter.DisplayName
		if inviterName == "" {
			inviterName = deref(inviter.Email)
		}
		token, digest, err := newSecret(invitationPrefix)
		if err != nil {
			return err
		}
		expires := time.Now().Add(InvitationTTL)
		message, err := notifications.Enqueue(ctx, tx, invitationEmail(address, role, expires, ws.Name, inviterName, i.invitationLink(token)))
		if err != nil {
			return err
		}
		messageID := uuid.UUID(message)
		inviterID := uuid.UUID(p.User)
		row, err := q.InsertInvitation(ctx, InsertInvitationParams{
			WorkspaceID: uuid.UUID(ws.ID), Email: address, Role: string(role), InvitedBy: &inviterID,
			TokenHash: digest, MessageID: &messageID, ExpiresAt: expires,
		})
		if errors.Is(err, pgx.ErrNoRows) {
			return &ConflictError{Message: fmt.Sprintf("an invitation for %s is already open; resend that one instead", address)}
		}
		if err != nil {
			return fmt.Errorf("insert invitation: %w", err)
		}
		inv = Invitation{
			ID: InvitationID(row.ID), Workspace: ws.ID, Email: address, Role: role, InvitedBy: &p.User,
			InvitedByName: inviterName, Delivery: notifications.StateQueued, ExpiresAt: expires,
			CreatedAt: row.CreatedAt, UpdatedAt: row.UpdatedAt,
		}
		return nil
	})
	if err != nil {
		return Invitation{}, fmt.Errorf("invite %s: %w", address, err)
	}
	return inv, nil
}

// ResendInvitation sends the offer again on a new link, which is also how
// an expired one returns. The previous link stops working, and its email
// is withdrawn if it has not gone yet. The email still names whoever made
// the offer.
func (i *Identity) ResendInvitation(ctx context.Context, p Principal, workspace string, id InvitationID) (Invitation, error) {
	var inv Invitation
	err := i.changeInvitation(ctx, p, workspace, id, func(tx pgx.Tx, q *Queries, ws Workspace, row LockInvitationRow) error {
		token, digest, err := newSecret(invitationPrefix)
		if err != nil {
			return err
		}
		expires := time.Now().Add(InvitationTTL)
		message, err := notifications.Enqueue(ctx, tx, invitationEmail(row.Email, Role(row.Role), expires, ws.Name, row.InvitedByName, i.invitationLink(token)))
		if err != nil {
			return err
		}
		if row.MessageID != nil {
			if err := notifications.Discard(ctx, tx, []notifications.MessageID{notifications.MessageID(*row.MessageID)}); err != nil {
				return err
			}
		}
		messageID := uuid.UUID(message)
		updated, err := q.ReissueInvitation(ctx, ReissueInvitationParams{TokenHash: digest, MessageID: &messageID, ExpiresAt: expires, ID: row.ID})
		if err != nil {
			return fmt.Errorf("reissue invitation: %w", err)
		}
		inv = Invitation{
			ID: id, Workspace: ws.ID, Email: row.Email, Role: Role(row.Role), InvitedBy: (*UserID)(row.InvitedBy),
			InvitedByName: row.InvitedByName, Delivery: notifications.StateQueued, ExpiresAt: expires,
			CreatedAt: row.CreatedAt, UpdatedAt: updated,
		}
		return nil
	})
	return inv, err
}

// RevokeInvitation withdraws an offer and its unsent email.
func (i *Identity) RevokeInvitation(ctx context.Context, p Principal, workspace string, id InvitationID) error {
	return i.changeInvitation(ctx, p, workspace, id, func(tx pgx.Tx, q *Queries, _ Workspace, row LockInvitationRow) error {
		if err := q.DeleteInvitation(ctx, row.ID); err != nil {
			return fmt.Errorf("delete invitation: %w", err)
		}
		if row.MessageID == nil {
			return nil
		}
		return notifications.Discard(ctx, tx, []notifications.MessageID{notifications.MessageID(*row.MessageID)})
	})
}

// changeInvitation runs change on a locked invitation of an active
// workspace the caller administers. An invitation of another workspace is
// reported as not found.
func (i *Identity) changeInvitation(ctx context.Context, p Principal, workspace string, id InvitationID,
	change func(pgx.Tx, *Queries, Workspace, LockInvitationRow) error,
) error {
	ws, err := i.AuthorizeWorkspaceRole(ctx, p, workspace, RoleAdministrator)
	if err != nil {
		return err
	}
	err = pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		if _, err := q.LockActiveWorkspace(ctx, uuid.UUID(ws.ID)); errors.Is(err, pgx.ErrNoRows) {
			return &ConflictError{Message: "the workspace is being deleted"}
		} else if err != nil {
			return fmt.Errorf("lock workspace: %w", err)
		}
		row, err := q.LockInvitation(ctx, LockInvitationParams{ID: uuid.UUID(id), WorkspaceID: uuid.UUID(ws.ID)})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock invitation: %w", err)
		}
		return change(tx, q, ws, row)
	})
	if err != nil {
		return fmt.Errorf("invitation %s: %w", id, err)
	}
	return nil
}

// ListInvitations lists a workspace's open offers, expired ones included,
// with the delivery state of each one's email. It takes an administrator:
// these are addresses of people who are not members.
func (i *Identity) ListInvitations(ctx context.Context, p Principal, workspace string) ([]Invitation, error) {
	ws, err := i.AuthorizeWorkspaceRole(ctx, p, workspace, RoleAdministrator)
	if err != nil {
		return nil, err
	}
	rows, err := i.queries.ListInvitations(ctx, uuid.UUID(ws.ID))
	if err != nil {
		return nil, fmt.Errorf("list invitations: %w", err)
	}
	var messages []notifications.MessageID
	for _, row := range rows {
		if row.MessageID != nil {
			messages = append(messages, notifications.MessageID(*row.MessageID))
		}
	}
	states, err := notifications.States(ctx, i.pool, messages)
	if err != nil {
		return nil, err
	}
	out := make([]Invitation, len(rows))
	for n, row := range rows {
		delivery := notifications.StateQueued
		if row.MessageID != nil {
			if state, ok := states[notifications.MessageID(*row.MessageID)]; ok {
				delivery = state
			}
		}
		out[n] = Invitation{
			ID: InvitationID(row.ID), Workspace: ws.ID, Email: row.Email, Role: Role(row.Role),
			InvitedBy: (*UserID)(row.InvitedBy), InvitedByName: row.InvitedByName, Expired: row.Expired,
			Delivery: delivery, ExpiresAt: row.ExpiresAt, CreatedAt: row.CreatedAt, UpdatedAt: row.UpdatedAt,
		}
	}
	return out, nil
}

// InvitationPreview is what an invitation link shows before it is
// answered. It carries no invitation id: the link is the whole claim.
type InvitationPreview struct {
	Workspace     WorkspaceID
	WorkspaceName string
	Email         string
	Role          Role
	InvitedByName string
	Expired       bool
	ExpiresAt     time.Time
}

// PreviewInvitation reads the offer a link opens without redeeming it, so
// mail scanners and link previews cannot spend it.
func (i *Identity) PreviewInvitation(ctx context.Context, token string) (InvitationPreview, error) {
	row, err := i.queries.InvitationByToken(ctx, HashToken(token))
	if errors.Is(err, pgx.ErrNoRows) {
		return InvitationPreview{}, ErrNotFound
	}
	if err != nil {
		return InvitationPreview{}, fmt.Errorf("read invitation: %w", err)
	}
	return InvitationPreview{
		Workspace: WorkspaceID(row.WorkspaceID), WorkspaceName: row.WorkspaceName, Email: row.Email,
		Role: Role(row.Role), InvitedByName: row.InvitedByName, Expired: row.Expired, ExpiresAt: row.ExpiresAt,
	}, nil
}

// AcceptInvitation redeems the link as the signed-in account, whatever its
// email: holding the link is the claim. The offer is deleted under its row
// lock, so it is redeemed once. A member already holding less than the
// offered role is raised to it.
func (i *Identity) AcceptInvitation(ctx context.Context, p Principal, token string) (Workspace, Member, error) {
	if err := p.requireAccount("accept invitations"); err != nil {
		return Workspace{}, Member{}, err
	}
	var (
		ws     Workspace
		member Member
	)
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		inv, active, err := redeemable(ctx, q, token)
		if err != nil {
			return err
		}
		offered := Role(inv.Role)
		current, err := q.LockMember(ctx, LockMemberParams{WorkspaceID: inv.WorkspaceID, UserID: uuid.UUID(p.User)})
		role, since := offered, time.Now()
		switch {
		case errors.Is(err, pgx.ErrNoRows):
			if err := admitMember(ctx, q, WorkspaceID(inv.WorkspaceID)); err != nil {
				return err
			}
			if err := q.InsertMember(ctx, InsertMemberParams{WorkspaceID: inv.WorkspaceID, UserID: uuid.UUID(p.User), Role: string(offered)}); err != nil {
				return fmt.Errorf("add member: %w", err)
			}
		case err != nil:
			return fmt.Errorf("lock member: %w", err)
		case Role(current.Role).covers(offered):
			role, since = Role(current.Role), current.CreatedAt
		default:
			since = current.CreatedAt
			if err := q.SetMemberRole(ctx, SetMemberRoleParams{Role: string(offered), WorkspaceID: inv.WorkspaceID, UserID: uuid.UUID(p.User)}); err != nil {
				return fmt.Errorf("raise member role: %w", err)
			}
		}
		if err := answer(ctx, tx, q, inv); err != nil {
			return err
		}
		profile, err := q.UserProfile(ctx, uuid.UUID(p.User))
		if err != nil {
			return fmt.Errorf("read member: %w", err)
		}
		ws = Workspace{ID: WorkspaceID(active.ID), Name: active.Name, State: WorkspaceActive, Role: role, CreatedAt: active.CreatedAt}
		member = Member{User: p.User, DisplayName: profile.DisplayName, Email: deref(profile.Email), Role: role, CreatedAt: since}
		return nil
	})
	if err != nil {
		return Workspace{}, Member{}, fmt.Errorf("accept invitation: %w", err)
	}
	return ws, member, nil
}

// DeclineInvitation refuses the offer a link opens and removes it.
func (i *Identity) DeclineInvitation(ctx context.Context, token string) error {
	err := pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		inv, _, err := redeemable(ctx, q, token)
		if err != nil {
			return err
		}
		return answer(ctx, tx, q, inv)
	})
	if err != nil {
		return fmt.Errorf("decline invitation: %w", err)
	}
	return nil
}

// redeemable locks the offer a link opens. An unknown link and a spent one
// answer the same, because a redeemed offer leaves no row.
//
// Lock order is workspace, then invitation, as in DeleteWorkspace, so a
// deletion and an answer to one of its invitations cannot deadlock. The
// workspace is read unlocked first; an offer of a deleting workspace is
// gone.
func redeemable(ctx context.Context, q *Queries, token string) (LockInvitationByTokenRow, LockActiveWorkspaceRow, error) {
	var (
		inv    LockInvitationByTokenRow
		active LockActiveWorkspaceRow
	)
	hash := HashToken(token)
	ws, err := q.InvitationWorkspace(ctx, hash)
	if errors.Is(err, pgx.ErrNoRows) {
		return inv, active, ErrNotFound
	}
	if err != nil {
		return inv, active, fmt.Errorf("read invitation: %w", err)
	}
	active, err = q.LockActiveWorkspace(ctx, ws)
	if errors.Is(err, pgx.ErrNoRows) {
		return inv, active, ErrNotFound
	}
	if err != nil {
		return inv, active, fmt.Errorf("lock workspace: %w", err)
	}
	inv, err = q.LockInvitationByToken(ctx, hash)
	if errors.Is(err, pgx.ErrNoRows) || (err == nil && inv.WorkspaceID != ws) {
		return inv, active, ErrNotFound
	}
	if err != nil {
		return inv, active, fmt.Errorf("lock invitation: %w", err)
	}
	if inv.Expired {
		return inv, active, &ConflictError{Message: "this invitation has expired; ask for a new one"}
	}
	return inv, active, nil
}

// answer removes an offer that was accepted or declined, and its email if
// it has not gone yet.
func answer(ctx context.Context, tx pgx.Tx, q *Queries, inv LockInvitationByTokenRow) error {
	if err := q.DeleteInvitation(ctx, inv.ID); err != nil {
		return fmt.Errorf("delete invitation: %w", err)
	}
	if inv.MessageID == nil {
		return nil
	}
	return notifications.Discard(ctx, tx, []notifications.MessageID{notifications.MessageID(*inv.MessageID)})
}

func (i *Identity) invitationLink(token string) string {
	return i.cfg.PublicURL + "/invitations/" + token
}

// normalizeEmail folds an address and checks it is one mailbox.
func normalizeEmail(value string) (string, error) {
	email := strings.ToLower(strings.TrimSpace(value))
	local, domain, ok := strings.Cut(email, "@")
	if len(email) > 320 || !ok || strings.Contains(domain, "@") || strings.ContainsAny(email, " \t\r\n") ||
		local == "" || !strings.Contains(domain, ".") || strings.HasPrefix(domain, ".") || strings.HasSuffix(domain, ".") {
		return "", &InvalidError{Message: "invitation email must be a single address such as name@example.com"}
	}
	return email, nil
}

// invitationEmail is what the invited person reads: who asked, into what,
// and the link that joins. The link joins whichever account opens it, so
// the message says to keep it private.
func invitationEmail(to string, role Role, expires time.Time, workspace, inviter, link string) notifications.Email {
	who := inviter
	if who == "" {
		who = "A " + displayName + " administrator"
	}
	described := "a member"
	if role == RoleAdministrator {
		described = "an administrator"
	}
	date := expires.UTC().Format("January 02, 2006")
	text := fmt.Sprintf("%s invited you to join the workspace %s on %s as %s.\n\n"+
		"Open this link to accept. You will be asked to sign in first if you are not already:\n%s\n\n"+
		"The link joins as whichever account you are signed in as, so keep it to yourself. "+
		"It expires on %s. If you were not expecting this, you can ignore this message.",
		who, workspace, displayName, described, link, date)
	h := html.EscapeString
	body := `<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,` +
		`Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px 24px;` +
		`color:#1a1a1a;line-height:1.5">` +
		`<h1 style="font-size:20px;margin:0 0 16px">` +
		`You have been invited to ` + h(workspace) + `</h1>` +
		`<p><strong>` + h(who) + `</strong> invited you to join the workspace ` +
		`<strong>` + h(workspace) + `</strong> on ` + h(displayName) + ` as ` + described + `.</p>` +
		`<p style="margin:24px 0"><a href="` + h(link) + `" style="display:inline-block;` +
		`background:#1a1a1a;color:#ffffff;padding:12px 20px;border-radius:6px;` +
		`text-decoration:none;font-weight:600">Accept invitation</a></p>` +
		`<p style="color:#555;font-size:14px">You will be asked to sign in first if you ` +
		`are not already. The link joins as whichever account you are signed in as, so ` +
		`keep it to yourself. It expires on ` + h(date) + `.</p>` +
		`<p style="color:#888;font-size:13px">If you were not expecting this, you can ignore ` +
		`this message.</p></div>`
	return notifications.Email{
		To:      to,
		Subject: fmt.Sprintf("%s invited you to %s on %s", who, workspace, displayName),
		HTML:    body,
		Text:    text,
	}
}
