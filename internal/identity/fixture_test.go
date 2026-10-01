package identity

import (
	"io"
	"log/slog"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity/identitytest"
)

const publicURL = "https://dashboard.test"

type gitHubAccount = identitytest.Account

type fixture struct {
	t    *testing.T
	pool *pgxpool.Pool
	id   *Identity
	gh   *identitytest.GitHub
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	pool := dbtest.New(t)
	gh := identitytest.NewGitHub(t, publicURL+GitHubCallbackPath)
	return &fixture{t: t, pool: pool, gh: gh, id: NewIdentity(pool, Config{PublicURL: publicURL, GitHub: GitHubConfig{
		ClientID: identitytest.ClientID, ClientSecret: identitytest.ClientSecret, OAuthURL: gh.URL, APIURL: gh.URL,
	}})}
}

// authorize consents on GitHub for start's sign-in.
func (f *fixture) authorize(start SignInStart, code string, account gitHubAccount) string {
	return f.gh.Authorize(start.AuthorizeURL, code, account)
}

// signIn runs a complete GitHub sign-in and returns the session principal.
func (f *fixture) signIn(account gitHubAccount) Principal {
	f.t.Helper()
	token := f.sessionToken(account, "code-"+account.Login)
	p, err := f.id.AuthenticateSession(f.t.Context(), token)
	if err != nil {
		f.t.Fatal(err)
	}
	f.unlimited(p.User)
	return p
}

// unlimited waives the account's billing, which gives it Business limits,
// so these tests are not held to plan limits; billing tests those.
func (f *fixture) unlimited(user UserID) {
	f.t.Helper()
	b := billing.NewBilling(f.pool, billing.Config{}, slog.New(slog.NewTextHandler(io.Discard, nil)))
	if _, err := b.SetComplimentary(f.t.Context(), uuid.UUID(user), true); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fixture) sessionToken(account gitHubAccount, code string) string {
	f.t.Helper()
	start, err := f.id.BeginSignIn("")
	if err != nil {
		f.t.Fatal(err)
	}
	state := f.authorize(start, code, account)
	session, err := f.id.CompleteSignIn(f.t.Context(), start.Cookie, state, code)
	if err != nil {
		f.t.Fatalf("sign in %s: %v", account.Login, err)
	}
	return session.Token
}

// account creates a user through the admin path and returns an
// unrestricted token principal for it.
func (f *fixture) account(email string, admin bool) Principal {
	f.t.Helper()
	user, err := f.id.CreateUser(f.t.Context(), email, admin)
	if err != nil {
		f.t.Fatal(err)
	}
	f.unlimited(user)
	return f.tokenPrincipal(email, "")
}

func (f *fixture) tokenPrincipal(email, workspace string) Principal {
	f.t.Helper()
	token, err := f.id.CreateToken(f.t.Context(), email, workspace, "test")
	if err != nil {
		f.t.Fatal(err)
	}
	p, err := f.id.Authenticate(f.t.Context(), token)
	if err != nil {
		f.t.Fatal(err)
	}
	return p
}

func (f *fixture) exec(sql string, args ...any) {
	f.t.Helper()
	if _, err := f.pool.Exec(f.t.Context(), sql, args...); err != nil {
		f.t.Fatalf("%s: %v", sql, err)
	}
}
