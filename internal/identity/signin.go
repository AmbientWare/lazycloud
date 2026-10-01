package identity

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// Sign-in timing. A browser session lasts SessionTTL from sign-in; a
// sign-in started but not finished is abandoned after SignInTTL.
const (
	SessionTTL = 12 * time.Hour
	SignInTTL  = 10 * time.Minute
	// GitHubCallbackPath is where GitHub returns the browser, under the
	// public URL.
	GitHubCallbackPath = "/auth/github/callback"
	sessionPrefix      = "lcs_"
)

// GitHubConfig is the GitHub App people sign in through. OAuthURL and
// APIURL default to github.com and api.github.com.
type GitHubConfig struct {
	ClientID     string
	ClientSecret string
	OAuthURL     string
	APIURL       string
}

// Sign-in failures the browser is told about by name.
var (
	// ErrSignInUnavailable means GitHub sign-in is not configured.
	ErrSignInUnavailable = errors.New("github sign-in is not configured")
	// ErrSignInState means the sign-in expired, was replayed or did not
	// start in this browser.
	ErrSignInState = errors.New("sign-in state is invalid")
	// ErrReturnPath means return_to is not a same-origin path.
	ErrReturnPath = errors.New("return path must be a path beginning with a single /")
	// ErrAccountDisabled means the account exists and is disabled.
	ErrAccountDisabled = errors.New("account is disabled")
)

// GitHubError is a failed call to GitHub. Refused means GitHub rejected
// this request and the same attempt will be rejected again; otherwise
// GitHub failed and a later attempt may succeed.
type GitHubError struct {
	Refused bool
	Message string
}

func (e *GitHubError) Error() string { return "github: " + e.Message }

type gitHub struct {
	cfg         GitHubConfig
	redirectURI string
	client      *http.Client
}

func newGitHub(cfg GitHubConfig, redirectURI string) *gitHub {
	if cfg.OAuthURL == "" {
		cfg.OAuthURL = "https://github.com"
	}
	if cfg.APIURL == "" {
		cfg.APIURL = "https://api.github.com"
	}
	return &gitHub{cfg: cfg, redirectURI: redirectURI, client: &http.Client{Timeout: 10 * time.Second}}
}

func (g *gitHub) configured() bool { return g.cfg.ClientID != "" && g.cfg.ClientSecret != "" }

// SignInStart is where to send the browser and the cookie to set first.
type SignInStart struct {
	AuthorizeURL string
	// Cookie holds the state, PKCE verifier and return path; it is set
	// HttpOnly for SignInTTL and read back by CompleteSignIn.
	Cookie string
}

// BeginSignIn starts a GitHub sign-in that returns to returnTo, a path on
// this origin. The state travels through GitHub and back; the verifier and
// return path stay in the browser's cookie, so nothing is stored until the
// sign-in completes.
func (i *Identity) BeginSignIn(returnTo string) (SignInStart, error) {
	if !i.github.configured() {
		return SignInStart{}, ErrSignInUnavailable
	}
	if err := CheckReturnPath(returnTo); err != nil {
		return SignInStart{}, err
	}
	state, err := randomString()
	if err != nil {
		return SignInStart{}, err
	}
	verifier, err := randomString()
	if err != nil {
		return SignInStart{}, err
	}
	challenge := sha256.Sum256([]byte(verifier))
	query := url.Values{
		"client_id":             {i.github.cfg.ClientID},
		"redirect_uri":          {i.github.redirectURI},
		"state":                 {state},
		"code_challenge":        {base64.RawURLEncoding.EncodeToString(challenge[:])},
		"code_challenge_method": {"S256"},
	}
	return SignInStart{
		AuthorizeURL: strings.TrimRight(i.github.cfg.OAuthURL, "/") + "/login/oauth/authorize?" + query.Encode(),
		Cookie:       state + "." + verifier + "." + base64.RawURLEncoding.EncodeToString([]byte(returnTo)),
	}, nil
}

// CheckReturnPath accepts only a path on this origin: anything that could
// name a host would make sign-in an open redirect.
func CheckReturnPath(path string) error {
	if path == "" {
		return nil
	}
	if len(path) > 512 || !strings.HasPrefix(path, "/") || strings.HasPrefix(path, "//") ||
		strings.ContainsAny(path, "\\ \t\r\n\x00") {
		return ErrReturnPath
	}
	for _, r := range path {
		if r < 0x20 || r == 0x7f {
			return ErrReturnPath
		}
	}
	parsed, err := url.Parse(path)
	if err != nil || parsed.Host != "" || parsed.Scheme != "" {
		return ErrReturnPath
	}
	for segment := range strings.SplitSeq(parsed.Path, "/") {
		if segment == ".." {
			return ErrReturnPath
		}
	}
	return nil
}

// Session is a new browser session. Token goes into the session cookie and
// is never readable again.
type Session struct {
	Token     string
	ExpiresAt time.Time
	// ReturnTo is where the browser asked to land, or empty.
	ReturnTo string
}

// CompleteSignIn finishes a GitHub sign-in: it checks state against the
// cookie BeginSignIn set, redeems code with GitHub, finds or creates the
// account and its owned workspace, and opens a session.
func (i *Identity) CompleteSignIn(ctx context.Context, cookie, state, code string) (Session, error) {
	if !i.github.configured() {
		return Session{}, ErrSignInUnavailable
	}
	parts := strings.Split(cookie, ".")
	if len(parts) != 3 || state == "" || code == "" ||
		subtle.ConstantTimeCompare([]byte(parts[0]), []byte(state)) != 1 {
		return Session{}, ErrSignInState
	}
	returnTo, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil || CheckReturnPath(string(returnTo)) != nil {
		return Session{}, ErrSignInState
	}
	profile, err := i.github.identify(ctx, code, parts[1])
	if err != nil {
		return Session{}, err
	}
	var session Session
	err = pgx.BeginFunc(ctx, i.pool, func(tx pgx.Tx) error {
		q := i.queries.WithTx(tx)
		user, err := resolveAccount(ctx, q, profile)
		if err != nil {
			return err
		}
		if _, err := q.OwnedWorkspace(ctx, uuid.UUID(user)); errors.Is(err, pgx.ErrNoRows) {
			name, err := availableWorkspaceName(ctx, q, profile.Login, user)
			if err != nil {
				return err
			}
			if _, err := addWorkspace(ctx, tx, name, user); err != nil {
				return err
			}
		} else if err != nil {
			return fmt.Errorf("read owned workspace: %w", err)
		}
		session, err = openSession(ctx, q, user)
		return err
	})
	if err != nil {
		return Session{}, fmt.Errorf("complete sign-in: %w", err)
	}
	session.ReturnTo = string(returnTo)
	return session, nil
}

// resolveAccount finds the account a GitHub identity reaches, linking an
// unlinked account with the same verified email, or creates one.
func resolveAccount(ctx context.Context, q *Queries, profile gitHubProfile) (UserID, error) {
	if err := q.LockGitHubSubject(ctx, profile.ID); err != nil {
		return UserID{}, fmt.Errorf("lock github identity: %w", err)
	}
	var email *string
	if profile.Email != "" {
		email = &profile.Email
	}
	update := func(id uuid.UUID, status string) (UserID, error) {
		if status != "active" {
			return UserID{}, ErrAccountDisabled
		}
		if err := q.UpdateGitHubProfile(ctx, UpdateGitHubProfileParams{
			DisplayName: profile.DisplayName, AvatarUrl: profile.AvatarURL, GithubUserID: &profile.ID,
			GithubLogin: profile.Login, Email: email, ID: id,
		}); err != nil {
			return UserID{}, fmt.Errorf("update account profile: %w", err)
		}
		return UserID(id), nil
	}
	existing, err := q.UserByGitHubID(ctx, &profile.ID)
	if err == nil {
		return update(existing.ID, existing.Status)
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return UserID{}, fmt.Errorf("read account: %w", err)
	}
	if email != nil {
		unlinked, err := q.UnlinkedUserByEmail(ctx, *email)
		if err == nil {
			return update(unlinked.ID, unlinked.Status)
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return UserID{}, fmt.Errorf("read account by email: %w", err)
		}
	}
	id, err := q.InsertGitHubUser(ctx, InsertGitHubUserParams{
		Email: email, DisplayName: profile.DisplayName, AvatarUrl: profile.AvatarURL,
		GithubUserID: &profile.ID, GithubLogin: profile.Login,
	})
	if err != nil {
		return UserID{}, fmt.Errorf("insert account: %w", err)
	}
	return UserID(id), nil
}

// availableWorkspaceName derives a workspace name from a GitHub login,
// adding -2 to -9 when taken.
func availableWorkspaceName(ctx context.Context, q *Queries, login string, user UserID) (string, error) {
	base := workspaceNameFrom(login)
	var candidates []string
	if base != "" {
		candidates = append(candidates, base)
		for n := 2; n <= 9; n++ {
			candidates = append(candidates, fmt.Sprintf("%s-%d", strings.TrimRight(base[:min(len(base), 60)], "-"), n))
		}
	}
	taken, err := q.TakenWorkspaceNames(ctx, candidates)
	if err != nil {
		return "", fmt.Errorf("read taken workspace names: %w", err)
	}
	used := map[string]bool{}
	for _, name := range taken {
		used[name] = true
	}
	for _, name := range candidates {
		if !used[name] {
			return name, nil
		}
	}
	return "workspace-" + user.String(), nil
}

// workspaceNameFrom normalizes a login to the workspace name pattern.
func workspaceNameFrom(login string) string {
	var b strings.Builder
	for _, r := range strings.ToLower(login) {
		if (r >= 'a' && r <= 'z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
		} else {
			b.WriteRune('-')
		}
	}
	name := strings.Trim(b.String(), "-")
	if name == "" {
		return ""
	}
	if name[0] < 'a' || name[0] > 'z' {
		name = "w-" + name
	}
	return strings.TrimRight(name[:min(len(name), 63)], "-")
}

func openSession(ctx context.Context, q *Queries, user UserID) (Session, error) {
	token, digest, err := newSecret(sessionPrefix)
	if err != nil {
		return Session{}, err
	}
	expires := time.Now().Add(SessionTTL)
	if _, err := q.InsertSession(ctx, InsertSessionParams{UserID: uuid.UUID(user), TokenHash: digest, ExpiresAt: expires}); err != nil {
		return Session{}, fmt.Errorf("insert session: %w", err)
	}
	return Session{Token: token, ExpiresAt: expires}, nil
}

// AuthenticateSession resolves a session cookie to its principal.
func (i *Identity) AuthenticateSession(ctx context.Context, token string) (Principal, error) {
	row, err := i.queries.AuthenticateSession(ctx, HashToken(token))
	if errors.Is(err, pgx.ErrNoRows) {
		return Principal{}, ErrUnauthenticated
	}
	if err != nil {
		return Principal{}, fmt.Errorf("authenticate session: %w", err)
	}
	id := SessionID(row.ID)
	return Principal{User: UserID(row.UserID), Email: deref(row.Email), IsAdmin: row.IsAdmin, Session: &id}, nil
}

// SignOut ends the browser session that made the request and no other.
func (i *Identity) SignOut(ctx context.Context, p Principal) error {
	if p.Session == nil {
		return &ConflictError{Message: "only a browser session signs out; revoke a token instead"}
	}
	if err := i.queries.DeleteSession(ctx, uuid.UUID(*p.Session)); err != nil {
		return fmt.Errorf("delete session: %w", err)
	}
	return nil
}

// User is an account's profile.
type User struct {
	ID          UserID
	Email       string
	DisplayName string
	AvatarURL   string
	GitHubLogin string
	IsAdmin     bool
	Status      UserStatus
	CreatedAt   time.Time
}

// Me returns the caller's profile.
func (i *Identity) Me(ctx context.Context, p Principal) (User, error) {
	row, err := i.queries.UserProfile(ctx, uuid.UUID(p.User))
	if errors.Is(err, pgx.ErrNoRows) {
		return User{}, ErrUnauthenticated
	}
	if err != nil {
		return User{}, fmt.Errorf("read profile: %w", err)
	}
	return userFrom(row), nil
}

type gitHubProfile struct {
	ID          int64
	Login       string
	DisplayName string
	AvatarURL   string
	// Email is the verified primary address, or empty.
	Email string
}

// identify trades the authorization code for a user access token, reads
// the account and its verified primary email, and drops the token.
func (g *gitHub) identify(ctx context.Context, code, verifier string) (gitHubProfile, error) {
	form := url.Values{
		"client_id": {g.cfg.ClientID}, "client_secret": {g.cfg.ClientSecret}, "code": {code},
		"redirect_uri": {g.redirectURI}, "code_verifier": {verifier},
	}
	var exchanged struct {
		AccessToken      string `json:"access_token"`
		Error            string `json:"error"`
		ErrorDescription string `json:"error_description"`
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, strings.TrimRight(g.cfg.OAuthURL, "/")+"/login/oauth/access_token", strings.NewReader(form.Encode()))
	if err != nil {
		return gitHubProfile{}, fmt.Errorf("build github token request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Accept", "application/json")
	if err := g.do(req, &exchanged); err != nil {
		return gitHubProfile{}, err
	}
	// GitHub answers a rejected code with 200 and an error field.
	if exchanged.Error != "" {
		return gitHubProfile{}, &GitHubError{Refused: true, Message: "authorization rejected: " + exchanged.Error}
	}
	if exchanged.AccessToken == "" {
		return gitHubProfile{}, &GitHubError{Message: "no access token returned"}
	}
	var account struct {
		ID        int64   `json:"id"`
		Login     string  `json:"login"`
		Name      *string `json:"name"`
		AvatarURL string  `json:"avatar_url"`
	}
	if err := g.get(ctx, "/user", exchanged.AccessToken, &account); err != nil {
		return gitHubProfile{}, err
	}
	if account.ID <= 0 {
		return gitHubProfile{}, &GitHubError{Message: "account has no id"}
	}
	var emails []struct {
		Email    string `json:"email"`
		Primary  bool   `json:"primary"`
		Verified bool   `json:"verified"`
	}
	if err := g.get(ctx, "/user/emails", exchanged.AccessToken, &emails); err != nil {
		var ghErr *GitHubError
		if errors.As(err, &ghErr) && ghErr.Refused {
			// The App lacks the "Email addresses (read-only)" permission;
			// treating that as "no email" would hide a deployment mistake.
			return gitHubProfile{}, &GitHubError{Message: "email addresses refused; the GitHub App needs the Email addresses (read-only) permission"}
		}
		return gitHubProfile{}, err
	}
	profile := gitHubProfile{ID: account.ID, Login: account.Login, DisplayName: account.Login, AvatarURL: account.AvatarURL}
	if account.Name != nil && *account.Name != "" {
		profile.DisplayName = *account.Name
	}
	for _, e := range emails {
		if e.Primary && e.Verified {
			profile.Email = e.Email
		}
	}
	return profile, nil
}

func (g *gitHub) get(ctx context.Context, path, accessToken string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(g.cfg.APIURL, "/")+path, nil)
	if err != nil {
		return fmt.Errorf("build github request: %w", err)
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("Authorization", "Bearer "+accessToken)
	req.Header.Set("X-GitHub-Api-Version", "2022-11-28")
	return g.do(req, out)
}

// do sends req and decodes a JSON answer. A 4xx is a refusal of this
// request; a 5xx or transport failure is GitHub's.
func (g *gitHub) do(req *http.Request, out any) error {
	resp, err := g.client.Do(req)
	if err != nil {
		return &GitHubError{Message: "request failed: " + err.Error()}
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return &GitHubError{Message: "read answer: " + err.Error()}
	}
	if resp.StatusCode >= 300 {
		return &GitHubError{
			Refused: resp.StatusCode >= 400 && resp.StatusCode < 500,
			Message: req.URL.Path + " answered " + strconv.Itoa(resp.StatusCode),
		}
	}
	if err := json.Unmarshal(data, out); err != nil {
		return &GitHubError{Message: req.URL.Path + " answered unreadable JSON"}
	}
	return nil
}

func randomString() (string, error) {
	var raw [32]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", fmt.Errorf("generate secret: %w", err)
	}
	return base64.RawURLEncoding.EncodeToString(raw[:]), nil
}
