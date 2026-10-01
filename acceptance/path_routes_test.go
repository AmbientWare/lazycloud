package acceptance

import (
	"bufio"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/api"
	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// dashboardOrigin is the PublicURL the harness's API is configured with.
const dashboardOrigin = "http://127.0.0.1"

// session opens a browser session for the workspace's owner.
func (p *platform) session() string {
	p.t.Helper()
	token := "lcs_acceptance-" + p.t.Name()
	if _, err := p.pool.Exec(p.t.Context(), `
insert into sessions (user_id, token_hash, expires_at)
select id, $1, now() + interval '1 hour' from users where email = 'dev@lazycloud.test'`, identity.HashToken(token)); err != nil {
		p.t.Fatal(err)
	}
	return token
}

// browser sends what the dashboard sends: the session cookie, and its
// origin on anything but a read.
func (p *platform) browser(method, path, session, body string, origin bool) *http.Response {
	p.t.Helper()
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req, err := http.NewRequestWithContext(p.t.Context(), method, p.api+path, reader)
	if err != nil {
		p.t.Fatal(err)
	}
	req.AddCookie(&http.Cookie{Name: api.SessionCookie, Value: session})
	req.AddCookie(&http.Cookie{Name: "theme", Value: "dark"})
	if origin {
		req.Header.Set("Origin", dashboardOrigin)
	}
	if body != "" {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		p.t.Fatal(err)
	}
	return resp
}

func readAll(t *testing.T, resp *http.Response) string {
	t.Helper()
	body, err := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if err != nil {
		t.Fatal(err)
	}
	return string(body)
}

// The dashboard reaches deployed endpoints and ASGI apps same-origin on the
// API host with its session, as the reference's API served them: the
// workload sees neither credential, streams stream, and another workspace's
// path is refused.
func TestWorkloadsAnswerOnTheAPIHostWithTheSession(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": endpointApp, "web.py": asgiApp})
	p.deploy("api_demo", endpointSpec(source, "count_words", "app:count_words", "/word-count", apitypes.HttpMethodPOST))
	p.deploy("web", asgiSpec(source, "service", "web:service", apitypes.HttpKindAsgi, 8))
	session := p.session()

	var endpoint apitypes.HttpWorkload
	if status := p.apiCall(http.MethodGet, "/v1/workspaces/ws/apps/api_demo/endpoints/count_words", nil, &endpoint); status != http.StatusOK {
		t.Fatalf("describe: %d", status)
	}
	if endpoint.InvokePath != "/v1/workspaces/ws/apps/api_demo/endpoints/count_words/invoke" ||
		endpoint.VersionInvokePath != "/v1/workspaces/ws/apps/api_demo/endpoints/count_words/versions/1/invoke" {
		t.Fatalf("invoke paths %q %q", endpoint.InvokePath, endpoint.VersionInvokePath)
	}
	for _, path := range []string{endpoint.InvokePath, endpoint.VersionInvokePath} {
		resp := p.browser(http.MethodPost, path+"/word-count", session, `{"text": "one two three"}`, true)
		if body := readAll(t, resp); resp.StatusCode != http.StatusOK || body != `{"words": 3}` || resp.Header.Get("X-Request-Id") == "" {
			t.Fatalf("POST %s with the session: %d %s", path, resp.StatusCode, body)
		}
	}
	// A mutation without the dashboard's origin is refused, as on every
	// cookie-authenticated operation.
	resp := p.browser(http.MethodPost, endpoint.InvokePath+"/word-count", session, `{"text": "a"}`, false)
	if readAll(t, resp); resp.StatusCode != http.StatusForbidden {
		t.Fatalf("POST without the dashboard origin: %d", resp.StatusCode)
	}
	// Without a credential there is nothing to authorize.
	req, _ := http.NewRequestWithContext(t.Context(), http.MethodPost, p.api+endpoint.InvokePath+"/word-count", strings.NewReader("{}"))
	anonymous, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if readAll(t, anonymous); anonymous.StatusCode != http.StatusUnauthorized {
		t.Fatalf("POST without a credential: %d", anonymous.StatusCode)
	}

	// An ASGI route sees neither the session nor a token, only the app's own
	// cookies and where it is mounted.
	base := "/v1/workspaces/ws/apps/web/asgi/service/invoke"
	resp = p.browser(http.MethodGet, base+"/headers", session, "", false)
	body := readAll(t, resp)
	var seen struct {
		Headers map[string]string `json:"headers"`
	}
	if resp.StatusCode != http.StatusOK || json.Unmarshal([]byte(body), &seen) != nil {
		t.Fatalf("GET headers with the session: %d %s", resp.StatusCode, body)
	}
	if strings.Contains(seen.Headers["cookie"], api.SessionCookie) || seen.Headers["cookie"] != "theme=dark" ||
		seen.Headers["authorization"] != "" || seen.Headers["x-forwarded-prefix"] != base {
		t.Fatalf("the app saw %v", seen.Headers)
	}
	// A bearer token works too, and is stripped the same way.
	if status, _, body := p.apiRaw(http.MethodGet, base+"/headers"); status != http.StatusOK || strings.Contains(body, p.token) {
		t.Fatalf("GET headers with a token: %d %s", status, body)
	}

	// Server-sent events stream through the API host.
	resp = p.browser(http.MethodGet, base+"/events", session, "", false)
	reader := bufio.NewReader(resp.Body)
	began := time.Now()
	first, err := reader.ReadString('\n')
	if err != nil || first != "data: 0\n" || time.Since(began) > 500*time.Millisecond {
		t.Fatalf("first event %q after %v (%v); want it before the stream ends", first, time.Since(began), err)
	}
	_ = resp.Body.Close()

	// Another workspace's path is refused, whatever its app is called.
	if _, err := p.pool.Exec(t.Context(), `insert into workspaces (name) values ('other')`); err != nil {
		t.Fatal(err)
	}
	resp = p.browser(http.MethodGet, "/v1/workspaces/other/apps/web/asgi/service/invoke/headers", session, "", false)
	if readAll(t, resp); resp.StatusCode != http.StatusForbidden && resp.StatusCode != http.StatusNotFound {
		t.Fatalf("another workspace's path: %d", resp.StatusCode)
	}
}

// apiRaw sends a request to the API host with the platform token.
func (p *platform) apiRaw(method, path string) (int, http.Header, string) {
	p.t.Helper()
	req, err := http.NewRequestWithContext(p.t.Context(), method, p.api+path, nil)
	if err != nil {
		p.t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+p.token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		p.t.Fatal(err)
	}
	return resp.StatusCode, resp.Header, readAll(p.t, resp)
}
