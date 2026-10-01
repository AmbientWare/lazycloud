package api_test

import (
	"bytes"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/api"
	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity/identitytest"
)

// browser sends requests the way the dashboard does: with cookies the
// server set and the dashboard's Origin on state-changing requests. The
// cookies are Secure, so they are carried by hand over the test's HTTP.
type browser struct {
	e       *env
	cookies map[string]string
	origin  string
}

func (e *env) browser() *browser {
	return &browser{e: e, cookies: map[string]string{}, origin: dashboardURL}
}

// answer is a response with its body already read and closed.
type answer struct {
	StatusCode int
	Header     http.Header
}

func (b *browser) do(method, path string, body any, out any) answer {
	b.e.t.Helper()
	var reader io.Reader
	if body != nil {
		data, err := json.Marshal(body)
		if err != nil {
			b.e.t.Fatal(err)
		}
		reader = bytes.NewReader(data)
	}
	req, err := http.NewRequestWithContext(b.e.t.Context(), method, b.e.url+path, reader)
	if err != nil {
		b.e.t.Fatal(err)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if b.origin != "" && method != http.MethodGet {
		req.Header.Set("Origin", b.origin)
	}
	for name, value := range b.cookies {
		req.AddCookie(&http.Cookie{Name: name, Value: value})
	}
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(req)
	if err != nil {
		b.e.t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	data, _ := io.ReadAll(resp.Body)
	for _, c := range resp.Cookies() {
		if !c.HttpOnly || !c.Secure || c.SameSite != http.SameSiteLaxMode || c.Path != "/" {
			b.e.t.Fatalf("cookie %s attributes %+v", c.Name, c)
		}
		if c.MaxAge < 0 {
			delete(b.cookies, c.Name)
		} else {
			b.cookies[c.Name] = c.Value
		}
	}
	if out != nil && len(data) > 0 {
		if err := json.Unmarshal(data, out); err != nil {
			b.e.t.Fatalf("%s %s: decode %q: %v", method, path, data, err)
		}
	}
	return answer{StatusCode: resp.StatusCode, Header: resp.Header}
}

// signIn follows the GitHub redirect flow and returns where the browser
// landed.
func (b *browser) signIn(returnTo string, account identitytest.Account) string {
	b.e.t.Helper()
	resp := b.do("GET", "/auth/github/start?return_to="+url.QueryEscape(returnTo), nil, nil)
	if resp.StatusCode != http.StatusFound || b.cookies[api.SignInCookie] == "" {
		b.e.t.Fatalf("start: %d %v", resp.StatusCode, b.cookies)
	}
	code := "code-" + account.Login
	state := b.e.github.Authorize(resp.Header.Get("Location"), code, account)
	resp = b.do("GET", "/auth/github/callback?code="+code+"&state="+url.QueryEscape(state), nil, nil)
	if resp.StatusCode != http.StatusSeeOther {
		b.e.t.Fatalf("callback: %d", resp.StatusCode)
	}
	if _, ok := b.cookies[api.SignInCookie]; ok {
		b.e.t.Fatal("sign-in cookie not cleared")
	}
	return resp.Header.Get("Location")
}

func TestBrowserSessionAndDeviceLogin(t *testing.T) {
	e := newEnv(t)
	b := e.browser()
	landed := b.signIn("/activate?code=BCDF-GHJK", identitytest.Account{ID: 42, Login: "dana", Name: "Dana", Email: "dana@example.com", Verified: true})
	if landed != "/activate?code=BCDF-GHJK" || b.cookies[api.SessionCookie] == "" {
		t.Fatalf("landed %q cookies %v", landed, b.cookies)
	}
	var me apitypes.Me
	if resp := b.do("GET", "/v1/me", nil, &me); resp.StatusCode != 200 || me.User.DisplayName != "Dana" ||
		len(me.Workspaces) != 1 || me.Workspaces[0].Name != "dana" || resp.Header.Get(api.RecommendedClientHeader) != "9.9.9" {
		t.Fatalf("me: %d %+v", resp.StatusCode, me)
	}

	// The CLI side needs no credential.
	cli := e.browser()
	var start apitypes.DeviceLogin
	if resp := cli.do("POST", "/v1/device-codes", map[string]string{"client_name": "cli@laptop"}, &start); resp.StatusCode != 201 ||
		start.VerificationUri != dashboardURL+"/activate" ||
		start.VerificationUriComplete != dashboardURL+"/activate?code="+start.UserCode || start.PollIntervalSeconds != 5 {
		t.Fatalf("start: %d %+v", resp.StatusCode, start)
	}
	var poll apitypes.DeviceTokenResponse
	if resp := cli.do("POST", "/v1/device-codes/token", map[string]string{"device_code": start.DeviceCode}, &poll); resp.StatusCode != 200 || poll.Status != apitypes.DeviceTokenStatusPending {
		t.Fatalf("poll: %d %+v", resp.StatusCode, poll)
	}

	// A cookie request that changes state must come from the dashboard.
	var apiErr apitypes.Error
	b.origin = "https://evil.test"
	if resp := b.do("POST", "/v1/device-codes/"+start.UserCode+"/approve", nil, &apiErr); resp.StatusCode != 403 {
		t.Fatalf("cross-origin approve: %d %+v", resp.StatusCode, apiErr)
	}
	b.origin = ""
	if resp := b.do("POST", "/v1/device-codes/"+start.UserCode+"/approve", nil, &apiErr); resp.StatusCode != 403 {
		t.Fatalf("approve without origin: %d", resp.StatusCode)
	}
	b.origin = dashboardURL
	var code apitypes.DeviceCode
	if resp := b.do("GET", "/v1/device-codes/"+strings.ToLower(strings.ReplaceAll(start.UserCode, "-", "")), nil, &code); resp.StatusCode != 200 || code.ClientName != "cli@laptop" {
		t.Fatalf("lookup: %d %+v", resp.StatusCode, code)
	}
	if resp := b.do("POST", "/v1/device-codes/"+start.UserCode+"/approve", nil, &code); resp.StatusCode != 200 || code.Status != apitypes.DeviceCodeStatusApproved {
		t.Fatalf("approve: %d %+v", resp.StatusCode, code)
	}
	if _, err := e.pool.Exec(t.Context(), "update device_codes set last_polled_at = now() - interval '1 minute'"); err != nil {
		t.Fatal(err)
	}
	if resp := cli.do("POST", "/v1/device-codes/token", map[string]string{"device_code": start.DeviceCode}, &poll); resp.StatusCode != 200 || poll.Status != apitypes.DeviceTokenStatusApproved || poll.Token == nil {
		t.Fatalf("approved poll: %d %+v", resp.StatusCode, poll)
	}
	var cliMe apitypes.Me
	if status := e.do("GET", "/v1/me", *poll.Token, nil, &cliMe); status != 200 || cliMe.User.Id != me.User.Id {
		t.Fatalf("device token me: %d %+v", status, cliMe)
	}

	// Signing out is for browser sessions, and ends only this one.
	if status := e.do("DELETE", "/v1/sessions/current", *poll.Token, nil, &apiErr); status != 401 {
		t.Fatalf("token sign-out: %d %+v", status, apiErr)
	}
	if resp := b.do("DELETE", "/v1/sessions/current", nil, nil); resp.StatusCode != 204 || b.cookies[api.SessionCookie] != "" {
		t.Fatalf("sign out: %d %v", resp.StatusCode, b.cookies)
	}
	if resp := b.do("GET", "/v1/me", nil, &apiErr); resp.StatusCode != 401 || apiErr.Code != apitypes.Unauthenticated {
		t.Fatalf("after sign-out: %d %+v", resp.StatusCode, apiErr)
	}
	if status := e.do("GET", "/v1/me", *poll.Token, nil, nil); status != 200 {
		t.Fatalf("device token after sign-out: %d", status)
	}
}

func TestSignInFailuresRedirect(t *testing.T) {
	e := newEnv(t)
	b := e.browser()
	cases := []struct{ path, want string }{
		{"/auth/github/start?return_to=//evil.test", "/signin?error=invalid_return_to"},
		{"/auth/github/callback?error=access_denied", "/signin?error=access_denied"},
		{"/auth/github/callback?code=c&state=s", "/signin?error=invalid_state"},
	}
	for _, c := range cases {
		resp := b.do("GET", c.path, nil, nil)
		if resp.StatusCode != http.StatusSeeOther || resp.Header.Get("Location") != c.want {
			t.Errorf("%s: %d %s", c.path, resp.StatusCode, resp.Header.Get("Location"))
		}
	}
	// The browser lands on the dashboard by default.
	if landed := b.signIn("", identitytest.Account{ID: 1, Login: "x", Email: "x@example.com", Verified: true}); landed != "/dashboard" {
		t.Fatalf("default landing %q", landed)
	}
	// Disabled accounts are told so.
	if _, err := e.pool.Exec(t.Context(), "update users set status = 'disabled' where github_user_id = 1"); err != nil {
		t.Fatal(err)
	}
	other := e.browser()
	resp := other.do("GET", "/auth/github/start", nil, nil)
	state := e.github.Authorize(resp.Header.Get("Location"), "again", identitytest.Account{ID: 1, Login: "x", Email: "x@example.com", Verified: true})
	resp = other.do("GET", "/auth/github/callback?code=again&state="+url.QueryEscape(state), nil, nil)
	if resp.Header.Get("Location") != "/signin?error=account_disabled" {
		t.Fatalf("disabled: %s", resp.Header.Get("Location"))
	}
}

func TestIdentityOperationsOverHTTP(t *testing.T) {
	e := newEnv(t)
	var apiErr apitypes.Error
	if status := e.do("GET", "/v1/tokens", "", nil, &apiErr); status != 401 || apiErr.Code != apitypes.Unauthenticated {
		t.Fatalf("no credential: %d %+v", status, apiErr)
	}
	var created apitypes.CreatedToken
	if status := e.do("POST", "/v1/tokens", e.owner, map[string]any{"name": "ci", "expires_in_seconds": 3600}, &apiErr); status != 400 {
		t.Fatalf("short expiry: %d %+v", status, apiErr)
	}
	if status := e.do("POST", "/v1/tokens", e.owner, map[string]any{"name": "ci", "expires_in_seconds": 86400 * 30}, &created); status != 201 || created.Record.ExpiresAt == nil || !strings.HasPrefix(created.Token, "lc_") {
		t.Fatalf("create token: %d %+v", status, created)
	}
	var list apitypes.TokenList
	if status := e.do("GET", "/v1/tokens?limit=1", e.owner, nil, &list); status != 200 || len(list.Tokens) != 1 || list.NextCursor == nil || list.Tokens[0].Status != apitypes.TokenStatusActive {
		t.Fatalf("list: %d %+v", status, list)
	}
	if status := e.do("DELETE", "/v1/tokens/"+created.Record.Id.String(), created.Token, nil, &apiErr); status != 409 {
		t.Fatalf("self revoke: %d %+v", status, apiErr)
	}
	if status := e.do("DELETE", "/v1/tokens/"+created.Record.Id.String(), e.owner, nil, nil); status != 204 {
		t.Fatalf("revoke: %d", status)
	}
	if status := e.do("GET", "/v1/me", created.Token, nil, nil); status != 401 {
		t.Fatalf("revoked token: %d", status)
	}

	// Roles map to forbidden with their reason.
	if status := e.do("POST", "/v1/workspaces", e.owner, map[string]string{"name": "more"}, &apiErr); status != 403 || !strings.Contains(apiErr.Message, "administrator") {
		t.Fatalf("member creates workspace: %d %+v", status, apiErr)
	}
	var inv apitypes.Invitation
	if status := e.do("POST", "/v1/workspaces/acme/invitations", e.owner, map[string]string{"email": "new@example.com"}, &inv); status != 201 || inv.Role != apitypes.InvitationRoleMember || inv.Delivery != apitypes.DeliveryStateQueued {
		t.Fatalf("invite: %d %+v", status, inv)
	}
	if status := e.do("GET", "/v1/workspaces/acme/invitations", e.outsider, nil, &apiErr); status != 403 {
		t.Fatalf("outsider invitations: %d", status)
	}
	var members apitypes.MemberList
	if status := e.do("GET", "/v1/workspaces/acme/members", e.owner, nil, &members); status != 200 || len(members.Members) != 1 || members.Members[0].Role != apitypes.WorkspaceRoleOwner {
		t.Fatalf("members: %d %+v", status, members)
	}
	if status := e.do("PATCH", "/v1/workspaces/acme/members/"+members.Members[0].UserId.String(), e.owner, map[string]string{"role": "owner"}, &apiErr); status != 400 {
		t.Fatalf("grant owner: %d %+v", status, apiErr)
	}
	var ws apitypes.Workspace
	if status := e.do("PATCH", "/v1/workspaces/acme", e.owner, map[string]string{"name": "acme-two"}, &ws); status != 200 || ws.Name != "acme-two" {
		t.Fatalf("rename: %d %+v", status, ws)
	}
	if status := e.do("DELETE", "/v1/workspaces/acme-two", e.owner, nil, &apiErr); status != 403 {
		t.Fatalf("member delete: %d", status)
	}
}

func TestResendWebhook(t *testing.T) {
	e := newEnv(t)
	if _, err := e.pool.Exec(t.Context(), `insert into email_outbox (recipient, subject, html, body_text, state, provider_message_id, settled_at)
		values ('a@example.com', 's', 'h', 't', 'sent', 're_1', now())`); err != nil {
		t.Fatal(err)
	}
	body := []byte(`{"type":"email.bounced","created_at":"2026-09-30T12:00:00Z","data":{"email_id":"re_1","bounce":{"subType":"Suppressed","message":"on the suppression list"}}}`)
	send := func(body []byte, sign bool) int {
		req, err := http.NewRequestWithContext(t.Context(), "POST", e.url+"/webhooks/resend", bytes.NewReader(body))
		if err != nil {
			t.Fatal(err)
		}
		ts := strconv.FormatInt(time.Now().Unix(), 10)
		key, _ := base64.StdEncoding.DecodeString(strings.TrimPrefix(webhookSecret, "whsec_"))
		mac := hmac.New(sha256.New, key)
		_, _ = fmt.Fprintf(mac, "msg_1.%s.%s", ts, body)
		signature := base64.StdEncoding.EncodeToString(mac.Sum(nil))
		if !sign {
			signature = base64.StdEncoding.EncodeToString([]byte("forged"))
		}
		req.Header.Set("svix-id", "msg_1")
		req.Header.Set("svix-timestamp", ts)
		req.Header.Set("svix-signature", "v1,old v1,"+signature)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		return resp.StatusCode
	}
	if status := send(body, false); status != 400 {
		t.Fatalf("forged: %d", status)
	}
	if status := send(body, true); status != 204 {
		t.Fatalf("signed: %d", status)
	}
	var state, detail string
	if err := e.pool.QueryRow(t.Context(), "select state, last_error from email_outbox").Scan(&state, &detail); err != nil || state != "bounced" || detail != "Suppressed on the suppression list" {
		t.Fatalf("recorded %s %q %v", state, detail, err)
	}
	// An older event does not undo a newer one; unknown types are
	// acknowledged.
	older := bytes.Replace(body, []byte(`"email.bounced","created_at":"2026-09-30T12:00:00Z"`), []byte(`"email.delivered","created_at":"2026-09-30T11:00:00Z"`), 1)
	if status := send(older, true); status != 204 {
		t.Fatalf("older: %d", status)
	}
	if status := send([]byte(`{"type":"email.opened","created_at":"2026-09-30T13:00:00Z","data":{"email_id":"re_1"}}`), true); status != 204 {
		t.Fatalf("unknown type: %d", status)
	}
	if err := e.pool.QueryRow(t.Context(), "select state from email_outbox").Scan(&state); err != nil || state != "bounced" {
		t.Fatalf("after older event %s %v", state, err)
	}
}
