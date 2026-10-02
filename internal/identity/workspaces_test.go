package identity

import (
	"errors"
	"strings"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgconn"

	"github.com/AmbientWare/lazycloud/internal/notifications"
)

func TestWorkspaceLifecycle(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	admin := f.account("admin@example.com", true)
	member := f.account("member@example.com", false)

	if _, err := f.id.CreateOwnedWorkspace(ctx, member, "mine", nil); !errors.Is(err, ErrAdminRequired) {
		t.Fatalf("member create: %v", err)
	}
	alpha, err := f.id.CreateOwnedWorkspace(ctx, admin, "alpha", nil)
	if err != nil || alpha.Role != RoleOwner || alpha.State != WorkspaceActive {
		t.Fatalf("create %+v %v", alpha, err)
	}
	if _, err := f.id.CreateOwnedWorkspace(ctx, admin, "alpha", nil); !errors.Is(err, ErrExists) {
		t.Fatalf("duplicate: %v", err)
	}
	// The last workspace stays.
	var conflict *ConflictError
	if _, err := f.id.DeleteWorkspace(ctx, admin, "alpha"); !errors.As(err, &conflict) {
		t.Fatalf("delete last: %v", err)
	}
	if _, err := f.id.CreateOwnedWorkspace(ctx, admin, "beta", nil); err != nil {
		t.Fatal(err)
	}
	f.exec(`insert into workspace_members (workspace_id, user_id, role) select w.id, u.id, 'member'
		from workspaces w, users u where w.name = 'alpha' and u.email = 'member@example.com'`)

	// Any member renames; names stay unique.
	renamed, err := f.id.RenameWorkspace(ctx, member, "alpha", "gamma")
	if err != nil || renamed.Name != "gamma" || renamed.Role != RoleMember {
		t.Fatalf("rename %+v %v", renamed, err)
	}
	if _, err := f.id.RenameWorkspace(ctx, member, "gamma", "beta"); !errors.As(err, &conflict) {
		t.Fatalf("rename onto taken name: %v", err)
	}
	if _, err := f.id.RenameWorkspace(ctx, member, "beta", "delta"); !errors.Is(err, ErrForbidden) {
		t.Fatalf("rename non-member: %v", err)
	}

	// Administrators list every workspace, page by page; members their own.
	page, err := f.id.ListWorkspaces(ctx, admin, WorkspacePage{Limit: 1})
	if err != nil || len(page.Workspaces) != 1 || page.Workspaces[0].Name != "beta" || page.Next != "beta" {
		t.Fatalf("admin page %+v %v", page, err)
	}
	page, _ = f.id.ListWorkspaces(ctx, admin, WorkspacePage{After: page.Next, Limit: 1})
	if len(page.Workspaces) != 1 || page.Workspaces[0].Name != "gamma" {
		t.Fatalf("admin second page %+v", page)
	}
	mine, _ := f.id.ListWorkspaces(ctx, member, WorkspacePage{Limit: 10})
	if len(mine.Workspaces) != 1 || mine.Workspaces[0].Name != "gamma" {
		t.Fatalf("member list %+v", mine)
	}

	// Deleting: only administrators, with an account credential.
	restricted := f.tokenPrincipal("member@example.com", "gamma")
	if _, err := f.id.DeleteWorkspace(ctx, member, "gamma"); !errors.Is(err, ErrAdminRequired) {
		t.Fatalf("member delete: %v", err)
	}
	if _, err := f.id.Invite(ctx, admin, "gamma", "guest@example.com", RoleMember); err != nil {
		t.Fatal(err)
	}
	deleting, err := f.id.DeleteWorkspace(ctx, admin, "gamma")
	if err != nil || deleting.State != WorkspaceDeleting {
		t.Fatalf("delete %+v %v", deleting, err)
	}
	// It refuses requests, its restricted tokens and invitations are gone,
	// and the unsent invitation email is withdrawn.
	if _, err := f.id.AuthorizeWorkspace(ctx, member, "gamma"); !errors.As(err, &conflict) {
		t.Fatalf("authorize deleting: %v", err)
	}
	var revoked bool
	if err := f.pool.QueryRow(ctx, "select revoked_at is not null from api_tokens where id = $1", uuid.UUID(*restricted.Token)).Scan(&revoked); err != nil || !revoked {
		t.Fatalf("restricted token revoked=%v %v", revoked, err)
	}
	var invitations int
	var state string
	if err := f.pool.QueryRow(ctx, `select (select count(*) from invitations), (select state from email_outbox)`).Scan(&invitations, &state); err != nil {
		t.Fatal(err)
	}
	if invitations != 0 || state != string(notifications.StateDiscarded) {
		t.Fatalf("invitations %d, email %s", invitations, state)
	}
	// Listing and reading still show it, so its deletion can be resumed.
	shown, err := f.id.GetWorkspace(ctx, member, "gamma")
	if err != nil || shown.State != WorkspaceDeleting {
		t.Fatalf("get deleting %+v %v", shown, err)
	}
	if again, err := f.id.DeleteWorkspace(ctx, admin, "gamma"); err != nil || again.State != WorkspaceDeleting {
		t.Fatalf("resume %+v %v", again, err)
	}
	listed, _ := f.id.DeletingWorkspaces(ctx, 10)
	if len(listed) != 1 || listed[0].Name != "gamma" {
		t.Fatalf("deleting list %+v", listed)
	}
	if removed, err := f.id.FinishWorkspaceDeletion(ctx, listed[0].ID); err != nil || !removed {
		t.Fatalf("finish deleting workspace: removed=%v %v", removed, err)
	}
	if _, err := f.id.GetWorkspace(ctx, admin, "gamma"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("finished deletion: %v", err)
	}
	// An active workspace is never removed by the finishing step.
	betaWS, _ := f.id.GetWorkspace(ctx, admin, "beta")
	if removed, err := f.id.FinishWorkspaceDeletion(ctx, betaWS.ID); err != nil || removed {
		t.Fatalf("finish active workspace: removed=%v %v", removed, err)
	}
	if _, err := f.id.GetWorkspace(ctx, admin, "beta"); err != nil {
		t.Fatalf("active workspace removed: %v", err)
	}
}

// Two deletions racing for the last two workspaces leave one.
func TestConcurrentDeletionKeepsOneWorkspace(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	admin := f.account("admin@example.com", true)
	for _, name := range []string{"one", "two"} {
		if _, err := f.id.CreateOwnedWorkspace(ctx, admin, name, nil); err != nil {
			t.Fatal(err)
		}
	}
	var wg sync.WaitGroup
	errs := make([]error, 2)
	for i, name := range []string{"one", "two"} {
		wg.Go(func() { _, errs[i] = f.id.DeleteWorkspace(ctx, admin, name) })
	}
	wg.Wait()
	var conflict *ConflictError
	if (errs[0] == nil) == (errs[1] == nil) || (!errors.As(errs[0], &conflict) && !errors.As(errs[1], &conflict)) {
		t.Fatalf("results %v", errs)
	}
}

func TestMembers(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	owner := f.account("owner@example.com", false)
	adminMember := f.account("helper@example.com", false)
	plain := f.account("plain@example.com", false)
	outsider := f.account("outsider@example.com", false)
	platform := f.account("platform@example.com", true)
	ws, err := f.id.CreateWorkspace(ctx, "acme", "owner@example.com")
	if err != nil {
		t.Fatal(err)
	}
	f.exec(`insert into workspace_members (workspace_id, user_id, role)
		select $1, id, case email when 'helper@example.com' then 'administrator' else 'member' end
		from users where email in ('helper@example.com', 'plain@example.com')`, uuid.UUID(ws.ID))

	members, err := f.id.ListMembers(ctx, plain, "acme")
	if err != nil || len(members) != 3 || members[0].Role != RoleOwner || members[0].Email != "owner@example.com" {
		t.Fatalf("members %+v %v", members, err)
	}
	if _, err := f.id.ListMembers(ctx, outsider, "acme"); !errors.Is(err, ErrForbidden) {
		t.Fatalf("outsider list: %v", err)
	}

	var roleErr *RoleError
	var conflict *ConflictError
	var invalid *InvalidError
	if _, err := f.id.SetMemberRole(ctx, plain, "acme", adminMember.User, RoleMember); !errors.As(err, &roleErr) {
		t.Fatalf("member changes role: %v", err)
	}
	if _, err := f.id.SetMemberRole(ctx, adminMember, "acme", owner.User, RoleMember); !errors.As(err, &conflict) {
		t.Fatalf("demote owner: %v", err)
	}
	if _, err := f.id.SetMemberRole(ctx, adminMember, "acme", plain.User, RoleOwner); !errors.As(err, &invalid) {
		t.Fatalf("grant owner: %v", err)
	}
	promoted, err := f.id.SetMemberRole(ctx, adminMember, "acme", plain.User, RoleAdministrator)
	if err != nil || promoted.Role != RoleAdministrator || promoted.Email != "plain@example.com" {
		t.Fatalf("promote %+v %v", promoted, err)
	}
	if _, err := f.id.SetMemberRole(ctx, adminMember, "acme", outsider.User, RoleMember); !errors.Is(err, ErrNotFound) {
		t.Fatalf("role of non-member: %v", err)
	}
	// A platform administrator acts without membership.
	if _, err := f.id.SetMemberRole(ctx, platform, "acme", plain.User, RoleMember); err != nil {
		t.Fatalf("platform admin: %v", err)
	}

	if err := f.id.RemoveMember(ctx, plain, "acme", adminMember.User); !errors.As(err, &roleErr) {
		t.Fatalf("member removes other: %v", err)
	}
	if err := f.id.RemoveMember(ctx, owner, "acme", owner.User); !errors.As(err, &conflict) {
		t.Fatalf("owner leaves: %v", err)
	}
	if err := f.id.RemoveMember(ctx, adminMember, "acme", owner.User); !errors.As(err, &conflict) {
		t.Fatalf("remove owner: %v", err)
	}
	// A member leaves without being an administrator.
	if err := f.id.RemoveMember(ctx, plain, "acme", plain.User); err != nil {
		t.Fatalf("leave: %v", err)
	}
	if _, err := f.id.AuthorizeWorkspace(ctx, plain, "acme"); !errors.Is(err, ErrForbidden) {
		t.Fatalf("after leaving: %v", err)
	}
	if err := f.id.RemoveMember(ctx, owner, "acme", adminMember.User); err != nil {
		t.Fatalf("remove: %v", err)
	}
	members, _ = f.id.ListMembers(ctx, owner, "acme")
	if len(members) != 1 {
		t.Fatalf("members left %+v", members)
	}
}

func TestInvitations(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	owner := f.signIn(gitHubAccount{ID: 1, Login: "owner", Name: "Olive Owner", Email: "owner@example.com", Verified: true})
	workspaces, _ := f.id.Workspaces(ctx, owner)
	ws := workspaces[0].Name
	plain := f.account("plain@example.com", false)
	guest := f.account("someone-else@example.com", false)
	f.exec(`insert into workspace_members (workspace_id, user_id, role)
		select w.id, u.id, 'member' from workspaces w, users u where w.name = $1 and u.email = 'plain@example.com'`, ws)

	var roleErr *RoleError
	if _, err := f.id.Invite(ctx, plain, ws, "new@example.com", RoleMember); !errors.As(err, &roleErr) {
		t.Fatalf("member invites: %v", err)
	}
	var invalid *InvalidError
	for _, bad := range []string{"no-at", "a@b", "two@@example.com", "sp ace@example.com"} {
		if _, err := f.id.Invite(ctx, owner, ws, bad, RoleMember); !errors.As(err, &invalid) {
			t.Fatalf("bad email %q: %v", bad, err)
		}
	}
	var conflict *ConflictError
	if _, err := f.id.Invite(ctx, owner, ws, "PLAIN@example.com", RoleMember); !errors.As(err, &conflict) {
		t.Fatalf("invite member: %v", err)
	}
	inv, err := f.id.Invite(ctx, owner, ws, " Guest@Example.com ", RoleAdministrator)
	if err != nil || inv.Email != "guest@example.com" || inv.InvitedByName != "Olive Owner" || inv.Delivery != notifications.StateQueued {
		t.Fatalf("invite %+v %v", inv, err)
	}
	if _, err := f.id.Invite(ctx, owner, ws, "guest@example.com", RoleMember); !errors.As(err, &conflict) {
		t.Fatalf("second invite: %v", err)
	}

	// The email is committed with the invitation and carries its link.
	link := f.invitationLink(inv.ID)
	token := strings.TrimPrefix(link, publicURL+"/invitations/")
	preview, err := f.id.PreviewInvitation(ctx, token)
	if err != nil || preview.WorkspaceName != ws || preview.Role != RoleAdministrator || preview.Expired {
		t.Fatalf("preview %+v %v", preview, err)
	}

	// Resending replaces the link and withdraws the unsent email.
	resent, err := f.id.ResendInvitation(ctx, owner, ws, inv.ID)
	if err != nil || resent.InvitedByName != "Olive Owner" {
		t.Fatalf("resend %+v %v", resent, err)
	}
	if _, err := f.id.PreviewInvitation(ctx, token); !errors.Is(err, ErrNotFound) {
		t.Fatalf("old link: %v", err)
	}
	var discarded, queued int
	if err := f.pool.QueryRow(ctx, `select count(*) filter (where state = 'discarded'), count(*) filter (where state = 'queued') from email_outbox`).Scan(&discarded, &queued); err != nil || discarded != 1 || queued != 1 {
		t.Fatalf("outbox discarded=%d queued=%d %v", discarded, queued, err)
	}
	token = strings.TrimPrefix(f.invitationLink(inv.ID), publicURL+"/invitations/")

	listed, err := f.id.ListInvitations(ctx, owner, ws)
	if err != nil || len(listed) != 1 || listed[0].Delivery != notifications.StateQueued {
		t.Fatalf("list %+v %v", listed, err)
	}
	if _, err := f.id.ListInvitations(ctx, plain, ws); !errors.As(err, &roleErr) {
		t.Fatalf("member lists invitations: %v", err)
	}
	// Another workspace's administrator cannot touch it.
	other := f.signIn(gitHubAccount{ID: 2, Login: "other", Email: "other@example.com", Verified: true})
	otherWS, _ := f.id.Workspaces(ctx, other)
	if err := f.id.RevokeInvitation(ctx, other, otherWS[0].Name, inv.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("revoke from other workspace: %v", err)
	}

	// Whoever opens the link joins, whatever their email; it works once.
	joined, member, err := f.id.AcceptInvitation(ctx, guest, token)
	if err != nil || joined.Name != ws || joined.CreatedAt.IsZero() || member.Role != RoleAdministrator || member.Email != "someone-else@example.com" {
		t.Fatalf("accept %+v %+v %v", joined, member, err)
	}
	if _, _, err := f.id.AcceptInvitation(ctx, guest, token); !errors.Is(err, ErrNotFound) {
		t.Fatalf("second accept: %v", err)
	}
	if _, err := f.id.AuthorizeWorkspaceRole(ctx, guest, ws, RoleAdministrator); err != nil {
		t.Fatalf("joined administrator: %v", err)
	}

	// An existing member is raised to the offered role, never lowered.
	up, _ := f.id.Invite(ctx, owner, ws, "elsewhere@example.com", RoleAdministrator)
	_, raised, err := f.id.AcceptInvitation(ctx, plain, strings.TrimPrefix(f.invitationLink(up.ID), publicURL+"/invitations/"))
	if err != nil || raised.Role != RoleAdministrator {
		t.Fatalf("raise %+v %v", raised, err)
	}
	down, _ := f.id.Invite(ctx, owner, ws, "down@example.com", RoleMember)
	_, kept, err := f.id.AcceptInvitation(ctx, plain, strings.TrimPrefix(f.invitationLink(down.ID), publicURL+"/invitations/"))
	if err != nil || kept.Role != RoleAdministrator {
		t.Fatalf("keep %+v %v", kept, err)
	}

	// Expired links are shown as expired and refused.
	late, _ := f.id.Invite(ctx, owner, ws, "late@example.com", RoleMember)
	lateToken := strings.TrimPrefix(f.invitationLink(late.ID), publicURL+"/invitations/")
	f.exec("update invitations set expires_at = now() - interval '1 second' where id = $1", uuid.UUID(late.ID))
	if p, err := f.id.PreviewInvitation(ctx, lateToken); err != nil || !p.Expired {
		t.Fatalf("expired preview %+v %v", p, err)
	}
	if _, _, err := f.id.AcceptInvitation(ctx, guest, lateToken); !errors.As(err, &conflict) {
		t.Fatalf("accept expired: %v", err)
	}
	if err := f.id.DeclineInvitation(ctx, lateToken); !errors.As(err, &conflict) {
		t.Fatalf("decline expired: %v", err)
	}
	// Resending brings an expired offer back.
	if _, err := f.id.ResendInvitation(ctx, owner, ws, late.ID); err != nil {
		t.Fatal(err)
	}
	lateToken = strings.TrimPrefix(f.invitationLink(late.ID), publicURL+"/invitations/")
	if err := f.id.DeclineInvitation(ctx, lateToken); err != nil {
		t.Fatalf("decline: %v", err)
	}

	revokable, _ := f.id.Invite(ctx, owner, ws, "revoke@example.com", RoleMember)
	if err := f.id.RevokeInvitation(ctx, owner, ws, revokable.ID); err != nil {
		t.Fatal(err)
	}
	if left, _ := f.id.ListInvitations(ctx, owner, ws); len(left) != 0 {
		t.Fatalf("open invitations %+v", left)
	}
}

// Concurrent accepts of one link admit one account.
func TestConcurrentAcceptIsSingleUse(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	owner := f.account("owner@example.com", false)
	if _, err := f.id.CreateWorkspace(ctx, "acme", "owner@example.com"); err != nil {
		t.Fatal(err)
	}
	inv, err := f.id.Invite(ctx, owner, "acme", "guest@example.com", RoleMember)
	if err != nil {
		t.Fatal(err)
	}
	token := strings.TrimPrefix(f.invitationLink(inv.ID), publicURL+"/invitations/")
	const n = 5
	people := make([]Principal, n)
	for i := range n {
		people[i] = f.account(string(rune('a'+i))+"@example.com", false)
	}
	var wg sync.WaitGroup
	joined := make([]bool, n)
	for i := range n {
		wg.Go(func() {
			_, _, err := f.id.AcceptInvitation(ctx, people[i], token)
			joined[i] = err == nil
			if err != nil && !errors.Is(err, ErrNotFound) {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	count := 0
	for _, ok := range joined {
		if ok {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("%d accepts succeeded", count)
	}
}

// Answering an invitation while its workspace is being deleted never
// deadlocks: both lock the workspace before the invitation.
func TestAnswerRacingDeletionDoesNotDeadlock(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	admin := f.account("admin@example.com", true)
	guest := f.account("guest@example.com", false)
	if _, err := f.id.CreateOwnedWorkspace(ctx, admin, "keep", nil); err != nil {
		t.Fatal(err)
	}
	for n := range 20 {
		name := "race-" + string(rune('a'+n))
		if _, err := f.id.CreateOwnedWorkspace(ctx, admin, name, nil); err != nil {
			t.Fatal(err)
		}
		inv, err := f.id.Invite(ctx, admin, name, "guest@example.com", RoleMember)
		if err != nil {
			t.Fatal(err)
		}
		token := strings.TrimPrefix(f.invitationLink(inv.ID), publicURL+"/invitations/")
		var wg sync.WaitGroup
		var deleteErr, answerErr error
		wg.Go(func() { _, deleteErr = f.id.DeleteWorkspace(ctx, admin, name) })
		wg.Go(func() {
			if n%2 == 0 {
				_, _, answerErr = f.id.AcceptInvitation(ctx, guest, token)
			} else {
				answerErr = f.id.DeclineInvitation(ctx, token)
			}
		})
		wg.Wait()
		var pgErr *pgconn.PgError
		for _, err := range []error{deleteErr, answerErr} {
			if errors.As(err, &pgErr) {
				t.Fatalf("round %d: %v", n, err)
			}
		}
		if deleteErr != nil || (answerErr != nil && !errors.Is(answerErr, ErrNotFound)) {
			t.Fatalf("round %d: delete %v, answer %v", n, deleteErr, answerErr)
		}
	}
}

// invitationLink reads the invitation's current link out of its email.
func (f *fixture) invitationLink(id InvitationID) string {
	f.t.Helper()
	var body string
	err := f.pool.QueryRow(f.t.Context(), `select o.body_text from invitations i join email_outbox o on o.id = i.message_id where i.id = $1`, uuid.UUID(id)).Scan(&body)
	if err != nil {
		f.t.Fatal(err)
	}
	start := strings.Index(body, publicURL+"/invitations/")
	if start < 0 {
		f.t.Fatalf("no link in %q", body)
	}
	return strings.Fields(body[start:])[0]
}
