package identity

import (
	"errors"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
)

func TestListUsersFiltersAndPages(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	admin := f.account("admin@example.com", true)
	octo := f.signIn(gitHubAccount{ID: 7, Login: "Octo_Cat", Name: "Octo Cat", Email: "octo@example.com", Verified: true})
	for _, email := range []string{"axb@example.com", "a_b@example.com", "carol@example.com"} {
		if _, err := f.id.CreateUser(ctx, email, false); err != nil {
			t.Fatal(err)
		}
	}
	f.exec(`update users set display_name = '50% Off' where email = 'carol@example.com'`)
	if _, err := f.id.SetUserStatus(ctx, admin, octo.User, UserDisabled); err != nil {
		t.Fatal(err)
	}

	emails := func(q UserQuery) []string {
		t.Helper()
		page, err := f.id.ListUsers(ctx, admin, q)
		if err != nil {
			t.Fatal(err)
		}
		var out []string
		for _, u := range page.Users {
			out = append(out, u.Email)
		}
		return out
	}

	// Pages of two walk every account once, oldest first, including ones
	// that never signed in.
	var walked []string
	q := UserQuery{Limit: 2}
	for {
		page, err := f.id.ListUsers(ctx, admin, q)
		if err != nil {
			t.Fatal(err)
		}
		for _, u := range page.Users {
			walked = append(walked, u.Email)
		}
		if page.Next == nil {
			break
		}
		q.After = page.Next
	}
	want := []string{"admin@example.com", "octo@example.com", "axb@example.com", "a_b@example.com", "carol@example.com"}
	if !slices.Equal(walked, want) {
		t.Fatalf("walk %v", walked)
	}

	// Search ignores case across name, email and login and takes LIKE
	// wildcards literally.
	disabled, admins := UserDisabled, PlatformAdministrator
	for _, tc := range []struct {
		q    UserQuery
		want []string
	}{
		{UserQuery{Search: " octo_CAT "}, []string{"octo@example.com"}},
		{UserQuery{Search: "OCTO cat"}, []string{"octo@example.com"}},
		{UserQuery{Search: "a_b"}, []string{"a_b@example.com"}},
		{UserQuery{Search: "%"}, []string{"carol@example.com"}},
		{UserQuery{Status: &disabled}, []string{"octo@example.com"}},
		{UserQuery{Role: &admins}, []string{"admin@example.com"}},
		{UserQuery{Role: &admins, Status: &disabled}, nil},
	} {
		tc.q.Limit = 50
		if got := emails(tc.q); !slices.Equal(got, tc.want) {
			t.Errorf("%+v: %v", tc.q, got)
		}
	}
	var invalid *InvalidError
	if _, err := f.id.ListUsers(ctx, admin, UserQuery{Search: strings.Repeat("x", MaxUserSearch+1), Limit: 50}); !errors.As(err, &invalid) {
		t.Fatalf("long search: %v", err)
	}
}

func TestAccountAdministrationNeedsAnAdministratorAccount(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	admin := f.account("admin@example.com", true)
	member := f.account("member@example.com", false)
	if _, err := f.id.CreateWorkspace(ctx, "acme", "admin@example.com"); err != nil {
		t.Fatal(err)
	}
	restricted := f.tokenPrincipal("admin@example.com", "acme")

	if _, err := f.id.ListUsers(ctx, member, UserQuery{Limit: 50}); !errors.Is(err, ErrAdminRequired) {
		t.Fatalf("member list: %v", err)
	}
	if _, err := f.id.SetUserRole(ctx, member, admin.User, PlatformMember); !errors.Is(err, ErrAdminRequired) {
		t.Fatalf("member demotes: %v", err)
	}
	var accountErr *AccountError
	if _, err := f.id.ListUsers(ctx, restricted, UserQuery{Limit: 50}); !errors.As(err, &accountErr) {
		t.Fatalf("restricted list: %v", err)
	}
	if _, err := f.id.SetUserStatus(ctx, restricted, member.User, UserDisabled); !errors.As(err, &accountErr) {
		t.Fatalf("restricted disable: %v", err)
	}

	// Nobody changes their own account, so the last administrator stays.
	var conflict *ConflictError
	if _, err := f.id.SetUserRole(ctx, admin, admin.User, PlatformMember); !errors.As(err, &conflict) {
		t.Fatalf("self demote: %v", err)
	}
	if _, err := f.id.SetUserStatus(ctx, admin, admin.User, UserDisabled); !errors.As(err, &conflict) {
		t.Fatalf("self disable: %v", err)
	}

	promoted, err := f.id.SetUserRole(ctx, admin, member.User, PlatformAdministrator)
	if err != nil || !promoted.IsAdmin || promoted.Status != UserActive {
		t.Fatalf("promote: %+v %v", promoted, err)
	}
	if _, err := f.id.SetUserRole(ctx, admin, UserID(uuid.New()), PlatformMember); !errors.Is(err, ErrNotFound) {
		t.Fatalf("unknown user: %v", err)
	}
}

func TestDisablingEndsCredentials(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	admin := f.account("admin@example.com", true)
	sessionToken := f.sessionToken(gitHubAccount{ID: 9, Login: "dev", Email: "dev@example.com", Verified: true}, "code-dev")
	person, err := f.id.AuthenticateSession(ctx, sessionToken)
	if err != nil {
		t.Fatal(err)
	}
	apiToken, err := f.id.CreateToken(ctx, "dev@example.com", "", "ci")
	if err != nil {
		t.Fatal(err)
	}
	// Approved in the browser, not yet collected by the CLI.
	start, err := f.id.StartDeviceLogin(ctx, "cli")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := f.id.ApproveDeviceLogin(ctx, person, start.UserCode); err != nil {
		t.Fatal(err)
	}

	disabled, err := f.id.SetUserStatus(ctx, admin, person.User, UserDisabled)
	if err != nil || disabled.Status != UserDisabled {
		t.Fatalf("disable: %+v %v", disabled, err)
	}
	enabled, err := f.id.SetUserStatus(ctx, admin, person.User, UserActive)
	if err != nil || enabled.Status != UserActive {
		t.Fatalf("enable: %+v %v", enabled, err)
	}
	// Enabling again restores none of the credentials disabling ended.
	if _, err := f.id.Authenticate(ctx, apiToken); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("token after re-enable: %v", err)
	}
	if _, err := f.id.AuthenticateSession(ctx, sessionToken); !errors.Is(err, ErrUnauthenticated) {
		t.Fatalf("session after re-enable: %v", err)
	}
	if poll, err := f.id.PollDeviceLogin(ctx, start.DeviceCode); err != nil || poll.Status != DeviceExpired || poll.Token != "" {
		t.Fatalf("device poll after re-enable: %+v %v", poll, err)
	}
	var live int
	if err := f.pool.QueryRow(ctx, `select (select count(*) from api_tokens where user_id = $1 and revoked_at is null)
		+ (select count(*) from sessions where user_id = $1)`, person.User.String()).Scan(&live); err != nil || live != 0 {
		t.Fatalf("live credentials %d %v", live, err)
	}
}

func TestConcurrentMutualDemotionKeepsAnAdministrator(t *testing.T) {
	f := newFixture(t)
	ctx := t.Context()
	a := f.account("a@example.com", true)
	b := f.account("b@example.com", true)

	// Hold both rows so the two demotions queue on the locks together.
	holder, err := f.pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = holder.Rollback(ctx) }()
	if _, err := holder.Exec(ctx, `select id from users for update`); err != nil {
		t.Fatal(err)
	}
	errs := make([]error, 2)
	var wg sync.WaitGroup
	for n, pair := range [][2]Principal{{a, b}, {b, a}} {
		wg.Go(func() { _, errs[n] = f.id.SetUserRole(ctx, pair[0], pair[1].User, PlatformMember) })
	}
	for deadline := time.Now().Add(10 * time.Second); ; {
		var waiting int
		if err := f.pool.QueryRow(ctx, `select count(*) from pg_stat_activity
			where datname = current_database() and wait_event_type = 'Lock'`).Scan(&waiting); err != nil {
			t.Fatal(err)
		}
		if waiting == 2 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("%d demotions waiting", waiting)
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := holder.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	wg.Wait()

	refused := 0
	for _, err := range errs {
		switch {
		case errors.Is(err, ErrAdminRequired):
			refused++
		case err != nil:
			t.Fatal(err)
		}
	}
	var admins int
	if err := f.pool.QueryRow(ctx, `select count(*) from users where is_admin and status = 'active'`).Scan(&admins); err != nil {
		t.Fatal(err)
	}
	if refused != 1 || admins != 1 {
		t.Fatalf("refused %d, administrators left %d", refused, admins)
	}
}
