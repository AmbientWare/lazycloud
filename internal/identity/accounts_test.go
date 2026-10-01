package identity

import (
	"errors"
	"log/slog"
	"sync"
	"testing"
	"time"
)

func TestGitHubSignIn(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	octo := gitHubAccount{ID: 101, Login: "Octo_Cat", Name: "Octo Cat", Email: "octo@example.com", Verified: true}

	// A first sign-in creates the account and an owned workspace named
	// after the login.
	p := f.signIn(octo)
	me, err := f.id.Me(ctx, p)
	if err != nil {
		t.Fatal(err)
	}
	if me.DisplayName != "Octo Cat" || me.Email != "octo@example.com" || me.GitHubLogin != "Octo_Cat" || me.IsAdmin {
		t.Fatalf("profile %+v", me)
	}
	workspaces, err := f.id.Workspaces(ctx, p)
	if err != nil || len(workspaces) != 1 || workspaces[0].Name != "octo-cat" || workspaces[0].Role != RoleOwner {
		t.Fatalf("workspaces %+v %v", workspaces, err)
	}

	// Signing in again reaches the same account and no new workspace; a
	// different login with the same normalized name gets a suffix.
	again := f.signIn(octo)
	if again.User != p.User {
		t.Fatalf("second sign-in made user %s, want %s", again.User, p.User)
	}
	other := f.signIn(gitHubAccount{ID: 102, Login: "octo-cat", Email: "second@example.com", Verified: true})
	ws, _ := f.id.Workspaces(ctx, other)
	if len(ws) != 1 || ws[0].Name != "octo-cat-2" {
		t.Fatalf("collision workspace %+v", ws)
	}
	again2, _ := f.id.Workspaces(ctx, again)
	if len(again2) != 1 {
		t.Fatalf("repeat sign-in workspaces %+v", again2)
	}

	// An account the admin command made is linked by its verified email
	// and keeps its administrator standing.
	if _, err := f.id.CreateUser(ctx, "Admin@Example.com", true); err != nil {
		t.Fatal(err)
	}
	admin := f.signIn(gitHubAccount{ID: 103, Login: "boss", Email: "admin@example.com", Verified: true})
	if !admin.IsAdmin {
		t.Fatalf("linked admin %+v", admin)
	}
	// An unverified primary email links nothing and is not stored.
	unverified := f.signIn(gitHubAccount{ID: 104, Login: "shady", Email: "admin@example.com", Verified: false})
	if unverified.IsAdmin || unverified.Email != "" {
		t.Fatalf("unverified email linked: %+v", unverified)
	}

	// A disabled account cannot sign in.
	f.exec("update users set status = 'disabled' where github_user_id = 101")
	start, err := f.id.BeginSignIn("/activate?code=BCDF-GHJK")
	if err != nil {
		t.Fatal(err)
	}
	state := f.authorize(start, "disabled-code", octo)
	if _, err := f.id.CompleteSignIn(ctx, start.Cookie, state, "disabled-code"); !errors.Is(err, ErrAccountDisabled) {
		t.Fatalf("disabled account: %v", err)
	}
	// Its sessions stop authenticating at once.
	sessionToken := f.sessionToken(gitHubAccount{ID: 105, Login: "temp", Email: "temp@example.com", Verified: true}, "temp")
	f.exec("update users set status = 'disabled' where github_user_id = 105")
	if _, err := f.id.AuthenticateSession(ctx, sessionToken); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("disabled session: %v", err)
	}
}

func TestSignInRefusals(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	account := gitHubAccount{ID: 7, Login: "seven", Email: "seven@example.com", Verified: true}

	for _, path := range []string{"//evil.test", "https://evil.test", "/a/../b", "/\\evil", "relative"} {
		if _, err := f.id.BeginSignIn(path); !errors.Is(err, ErrReturnPath) {
			t.Errorf("return path %q: %v", path, err)
		}
	}
	start, err := f.id.BeginSignIn("/w/acme")
	if err != nil {
		t.Fatal(err)
	}
	state := f.authorize(start, "c1", account)
	// A state that did not start in this browser is refused before GitHub
	// is asked anything.
	if _, err := f.id.CompleteSignIn(ctx, start.Cookie, state+"x", "c1"); !errors.Is(err, ErrSignInState) {
		t.Fatalf("forged state: %v", err)
	}
	other, _ := f.id.BeginSignIn("")
	if _, err := f.id.CompleteSignIn(ctx, other.Cookie, state, "c1"); !errors.Is(err, ErrSignInState) {
		t.Fatalf("another browser's cookie: %v", err)
	}
	// GitHub refusing the code is a refusal, not an outage.
	refused := account
	refused.Refuse = true
	start2, _ := f.id.BeginSignIn("")
	state2 := f.authorize(start2, "c2", refused)
	var ghErr *GitHubError
	if _, err := f.id.CompleteSignIn(ctx, start2.Cookie, state2, "c2"); !errors.As(err, &ghErr) || !ghErr.Refused {
		t.Fatalf("refused code: %v", err)
	}
	session, err := f.id.CompleteSignIn(ctx, start.Cookie, state, "c1")
	if err != nil || session.ReturnTo != "/w/acme" || time.Until(session.ExpiresAt) < SessionTTL-time.Minute {
		t.Fatalf("session %+v %v", session, err)
	}
	unconfigured := NewIdentity(f.pool, Config{PublicURL: publicURL})
	if _, err := unconfigured.BeginSignIn(""); !errors.Is(err, ErrSignInUnavailable) {
		t.Fatalf("unconfigured: %v", err)
	}
}

// Concurrent first sign-ins of one GitHub account create one user and one
// workspace.
func TestConcurrentFirstSignIn(t *testing.T) {
	f := newFixture(t)
	account := gitHubAccount{ID: 55, Login: "racer", Email: "racer@example.com", Verified: true}
	const n = 6
	users := make([]UserID, n)
	var wg sync.WaitGroup
	for i := range n {
		start, err := f.id.BeginSignIn("")
		if err != nil {
			t.Fatal(err)
		}
		code := "race-" + string(rune('a'+i))
		state := f.authorize(start, code, account)
		wg.Go(func() {
			session, err := f.id.CompleteSignIn(t.Context(), start.Cookie, state, code)
			if err != nil {
				t.Error(err)
				return
			}
			p, err := f.id.AuthenticateSession(t.Context(), session.Token)
			if err != nil {
				t.Error(err)
				return
			}
			users[i] = p.User
		})
	}
	wg.Wait()
	for _, u := range users[1:] {
		if u != users[0] {
			t.Fatalf("users %v", users)
		}
	}
	var accounts, workspaces int
	if err := f.pool.QueryRow(t.Context(), "select (select count(*) from users), (select count(*) from workspaces)").Scan(&accounts, &workspaces); err != nil {
		t.Fatal(err)
	}
	if accounts != 1 || workspaces != 1 {
		t.Fatalf("%d users, %d workspaces", accounts, workspaces)
	}
}

func TestSignOutEndsOnlyThatSession(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	account := gitHubAccount{ID: 9, Login: "nine", Email: "nine@example.com", Verified: true}
	first := f.sessionToken(account, "browser-1")
	second := f.sessionToken(account, "browser-2")
	p, err := f.id.AuthenticateSession(ctx, first)
	if err != nil {
		t.Fatal(err)
	}
	if err := f.id.SignOut(ctx, p); err != nil {
		t.Fatal(err)
	}
	if _, err := f.id.AuthenticateSession(ctx, first); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("signed-out session: %v", err)
	}
	if _, err := f.id.AuthenticateSession(ctx, second); err != nil {
		t.Fatalf("other session: %v", err)
	}
	token := f.tokenPrincipal("nine@example.com", "")
	var conflict *ConflictError
	if err := f.id.SignOut(ctx, token); !errors.As(err, &conflict) {
		t.Fatalf("token sign-out: %v", err)
	}
	// An expired session stops working and housekeeping removes it.
	f.exec("update sessions set expires_at = now() - interval '1 second'")
	if _, err := f.id.AuthenticateSession(ctx, second); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("expired session: %v", err)
	}
	if err := f.id.Housekeeping(ctx, slog.New(slog.DiscardHandler)); err != nil {
		t.Fatal(err)
	}
	var left int
	if err := f.pool.QueryRow(ctx, "select count(*) from sessions").Scan(&left); err != nil || left != 0 {
		t.Fatalf("sessions left %d %v", left, err)
	}
}

func TestDeviceLogin(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	person := f.signIn(gitHubAccount{ID: 1, Login: "dev", Email: "dev@example.com", Verified: true})

	start, err := f.id.StartDeviceLogin(ctx, "  cli@laptop ")
	if err != nil {
		t.Fatal(err)
	}
	if len(start.UserCode) != 9 || start.UserCode[4] != '-' || start.Interval != 5*time.Second || start.ExpiresIn != 15*time.Minute {
		t.Fatalf("start %+v", start)
	}
	poll := func() DevicePoll {
		t.Helper()
		p, err := f.id.PollDeviceLogin(ctx, start.DeviceCode)
		if err != nil {
			t.Fatal(err)
		}
		return p
	}
	if p := poll(); p.Status != DevicePending || p.Token != "" {
		t.Fatalf("first poll %+v", p)
	}
	// Polling again at once is too soon.
	if p := poll(); p.Status != DeviceSlowDown || p.Interval != 10*time.Second {
		t.Fatalf("early poll %+v", p)
	}
	code, err := f.id.DeviceLogin(ctx, "  "+lower(start.UserCode[:4])+start.UserCode[5:])
	if err != nil || code.ClientName != "cli@laptop" || code.Status != DevicePending {
		t.Fatalf("lookup %+v %v", code, err)
	}
	// A token restricted to one workspace cannot grant account access.
	ws, _ := f.id.Workspaces(ctx, person)
	restricted := f.tokenPrincipal("dev@example.com", ws[0].Name)
	var accountErr *AccountError
	if _, err := f.id.ApproveDeviceLogin(ctx, restricted, start.UserCode); !errors.As(err, &accountErr) {
		t.Fatalf("restricted approve: %v", err)
	}
	if _, err := f.id.ApproveDeviceLogin(ctx, person, start.UserCode); err != nil {
		t.Fatal(err)
	}
	var conflict *ConflictError
	if _, err := f.id.DenyDeviceLogin(ctx, start.UserCode); !errors.As(err, &conflict) {
		t.Fatalf("deny after approve: %v", err)
	}
	f.exec("update device_codes set last_polled_at = now() - interval '1 minute'")
	approved := poll()
	if approved.Status != DeviceApproved || approved.Token == "" {
		t.Fatalf("approved poll %+v", approved)
	}
	device, err := f.id.Authenticate(ctx, approved.Token)
	if err != nil || device.User != person.User || device.TokenWorkspace != nil {
		t.Fatalf("device token %+v %v", device, err)
	}
	// The restricted token stays listed; the device token only with
	// include_device.
	page, err := f.id.ListTokens(ctx, person, false, nil, 10)
	if err != nil || len(page.Tokens) != 1 || page.Tokens[0].Device || page.Tokens[0].Workspace == nil {
		t.Fatalf("tokens without device %+v %v", page, err)
	}
	page, _ = f.id.ListTokens(ctx, person, true, nil, 10)
	if len(page.Tokens) != 2 || !page.Tokens[0].Device || page.Tokens[0].Name != "cli@laptop" {
		t.Fatalf("device tokens %+v", page)
	}
	// The code is spent.
	if _, err := f.id.PollDeviceLogin(ctx, start.DeviceCode); !errors.As(err, &conflict) {
		t.Fatalf("second claim: %v", err)
	}

	denied, _ := f.id.StartDeviceLogin(ctx, "")
	if _, err := f.id.DenyDeviceLogin(ctx, denied.UserCode); err != nil {
		t.Fatal(err)
	}
	if p, err := f.id.PollDeviceLogin(ctx, denied.DeviceCode); err != nil || p.Status != DeviceDenied {
		t.Fatalf("denied poll %+v %v", p, err)
	}

	expired, _ := f.id.StartDeviceLogin(ctx, "")
	f.exec("update device_codes set expires_at = now() - interval '1 second' where user_code = $1", expired.UserCode)
	if p, err := f.id.PollDeviceLogin(ctx, expired.DeviceCode); err != nil || p.Status != DeviceExpired {
		t.Fatalf("expired poll %+v %v", p, err)
	}
	if _, err := f.id.ApproveDeviceLogin(ctx, person, expired.UserCode); !errors.As(err, &conflict) {
		t.Fatalf("approve expired: %v", err)
	}
	if _, err := f.id.PollDeviceLogin(ctx, "dc_unknown"); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown device code: %v", err)
	}
	var invalid *InvalidError
	if _, err := f.id.DeviceLogin(ctx, "BCD"); !errors.As(err, &invalid) {
		t.Fatalf("short code: %v", err)
	}
}

func lower(s string) string {
	b := []byte(s)
	for i, c := range b {
		if c >= 'A' && c <= 'Z' {
			b[i] = c + 'a' - 'A'
		}
	}
	return string(b)
}

func TestAccountTokens(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	owner := f.account("owner@example.com", false)

	day := 24 * time.Hour
	for _, bad := range []time.Duration{time.Hour, 91 * day} {
		var invalid *InvalidError
		if _, _, err := f.id.CreateAccountToken(ctx, owner, "x", &bad); !errors.As(err, &invalid) {
			t.Fatalf("lifetime %s: %v", bad, err)
		}
	}
	week := 7 * day
	secrets := map[string]string{}
	ids := map[string]TokenID{}
	for _, name := range []string{"a", "b", "c"} {
		var lifetime *time.Duration
		if name == "b" {
			lifetime = &week
		}
		secret, token, err := f.id.CreateAccountToken(ctx, owner, name, lifetime)
		if err != nil {
			t.Fatal(err)
		}
		secrets[name], ids[name] = secret, token.ID
	}
	// Pages are newest first and end with no cursor; owner's own test
	// token is the oldest.
	first, err := f.id.ListTokens(ctx, owner, true, nil, 2)
	if err != nil || len(first.Tokens) != 2 || first.Tokens[0].Name != "c" || first.Next == nil {
		t.Fatalf("first page %+v %v", first, err)
	}
	second, _ := f.id.ListTokens(ctx, owner, true, first.Next, 2)
	if len(second.Tokens) != 2 || second.Tokens[0].Name != "a" || second.Tokens[1].Name != "test" || second.Next != nil {
		t.Fatalf("second page %+v", second)
	}
	if first.Tokens[1].ExpiresAt == nil || time.Until(*first.Tokens[1].ExpiresAt) < week-time.Minute {
		t.Fatalf("expiry %+v", first.Tokens[1])
	}

	// A token cannot revoke itself; another account's token is not found.
	var conflict *ConflictError
	if err := f.id.RevokeToken(ctx, owner, *owner.Token); !errors.As(err, &conflict) {
		t.Fatalf("self revoke: %v", err)
	}
	outsider := f.account("outsider@example.com", false)
	if err := f.id.RevokeToken(ctx, outsider, ids["c"]); !errors.Is(err, ErrNotFound) {
		t.Fatalf("outsider revoke: %v", err)
	}
	if err := f.id.RevokeToken(ctx, owner, ids["c"]); err != nil {
		t.Fatal(err)
	}
	if _, err := f.id.Authenticate(ctx, secrets["c"]); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("revoked token: %v", err)
	}

	// An expired token stops authenticating but stays listed.
	f.exec("update api_tokens set expires_at = now() - interval '1 second' where name = 'b'")
	if _, err := f.id.Authenticate(ctx, secrets["b"]); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("expired token: %v", err)
	}
	if _, err := f.id.Authenticate(ctx, secrets["a"]); err != nil {
		t.Fatal(err)
	}
	page, _ := f.id.ListTokens(ctx, owner, true, nil, 10)
	if len(page.Tokens) != 3 {
		t.Fatalf("after revoke %+v", page)
	}

	// Use is recorded in memory and written in one batch.
	if page.Tokens[len(page.Tokens)-1].LastUsedAt != nil {
		t.Fatalf("last used before flush: %+v", page.Tokens)
	}
	if err := f.id.FlushTokenUse(ctx); err != nil {
		t.Fatal(err)
	}
	page, _ = f.id.ListTokens(ctx, owner, true, nil, 10)
	used := 0
	for _, token := range page.Tokens {
		if token.LastUsedAt != nil {
			used++
		}
	}
	// "test" (owner's principal) and the surviving unexpired token.
	if used != 2 {
		t.Fatalf("last used after flush: %+v", page.Tokens)
	}

	ws, err := f.id.CreateWorkspace(ctx, "solo", "owner@example.com")
	if err != nil {
		t.Fatal(err)
	}
	restricted := f.tokenPrincipal("owner@example.com", ws.Name)
	var accountErr *AccountError
	if _, _, err := f.id.CreateAccountToken(ctx, restricted, "x", nil); !errors.As(err, &accountErr) {
		t.Fatalf("restricted create: %v", err)
	}
}
