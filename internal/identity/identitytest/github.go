// Package identitytest plays GitHub at the OAuth provider boundary for
// sign-in tests.
package identitytest

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
)

// Client credentials the stub accepts.
const (
	ClientID     = "client"
	ClientSecret = "secret"
)

// Account is a GitHub account the stub signs in.
type Account struct {
	ID       int64
	Login    string
	Name     string
	Email    string
	Verified bool
	// Refuse makes the code exchange answer with an OAuth error.
	Refuse bool
}

// GitHub redeems a code only with the PKCE verifier whose challenge the
// authorize URL carried, and only for the expected redirect URI.
type GitHub struct {
	t           *testing.T
	URL         string
	redirectURI string
	mu          sync.Mutex
	accounts    map[string]Account // by code
	challenges  map[string]string  // by code
}

// NewGitHub starts a stub that expects redirectURI.
func NewGitHub(t *testing.T, redirectURI string) *GitHub {
	g := &GitHub{t: t, redirectURI: redirectURI, accounts: map[string]Account{}, challenges: map[string]string{}}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /login/oauth/access_token", g.exchange)
	mux.HandleFunc("GET /user", func(w http.ResponseWriter, r *http.Request) {
		if a, ok := g.account(r); ok {
			writeJSON(w, map[string]any{"id": a.ID, "login": a.Login, "name": a.Name, "avatar_url": "https://avatars.test/" + a.Login})
			return
		}
		http.Error(w, "bad token", http.StatusUnauthorized)
	})
	mux.HandleFunc("GET /user/emails", func(w http.ResponseWriter, r *http.Request) {
		if a, ok := g.account(r); ok {
			writeJSON(w, []map[string]any{
				{"email": "other@" + a.Login + ".test", "primary": false, "verified": true},
				{"email": a.Email, "primary": true, "verified": a.Verified},
			})
			return
		}
		http.Error(w, "bad token", http.StatusUnauthorized)
	})
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	g.URL = server.URL
	return g
}

func (g *GitHub) exchange(w http.ResponseWriter, r *http.Request) {
	if err := r.ParseForm(); err != nil || r.Form.Get("client_id") != ClientID || r.Form.Get("client_secret") != ClientSecret {
		http.Error(w, "bad client", http.StatusUnauthorized)
		return
	}
	code := r.Form.Get("code")
	g.mu.Lock()
	account, ok := g.accounts[code]
	challenge := g.challenges[code]
	g.mu.Unlock()
	sum := sha256.Sum256([]byte(r.Form.Get("code_verifier")))
	if !ok || account.Refuse || challenge != base64.RawURLEncoding.EncodeToString(sum[:]) ||
		r.Form.Get("redirect_uri") != g.redirectURI {
		writeJSON(w, map[string]string{"error": "bad_verification_code"})
		return
	}
	writeJSON(w, map[string]string{"access_token": "gho_" + code})
}

func (g *GitHub) account(r *http.Request) (Account, bool) {
	code, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer gho_")
	g.mu.Lock()
	defer g.mu.Unlock()
	a, found := g.accounts[code]
	return a, ok && found
}

// Authorize plays the person consenting on GitHub: it checks the authorize
// URL, issues code for account and returns the state GitHub sends back.
func (g *GitHub) Authorize(authorizeURL, code string, account Account) (state string) {
	g.t.Helper()
	parsed, err := url.Parse(authorizeURL)
	if err != nil {
		g.t.Fatal(err)
	}
	q := parsed.Query()
	if !strings.HasPrefix(authorizeURL, g.URL+"/login/oauth/authorize?") || q.Get("client_id") != ClientID ||
		q.Get("code_challenge_method") != "S256" || q.Get("redirect_uri") != g.redirectURI {
		g.t.Fatalf("authorize URL %s", authorizeURL)
	}
	g.mu.Lock()
	g.accounts[code] = account
	g.challenges[code] = q.Get("code_challenge")
	g.mu.Unlock()
	return q.Get("state")
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}
