//go:build hostruntime

package acceptance

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/control"
)

const endpointApp = `
import lazycloud
from pydantic import BaseModel

app = lazycloud.App("api_demo")


class WordCount(BaseModel):
    words: int


@app.endpoint(name="count_words", route="/word-count", methods=["POST"])
def count_words(text: str) -> WordCount:
    print("counting", text, flush=True)
    return WordCount(words=len(text.split()))
`

func endpointSpec(source, name, handler, route string, methods ...apitypes.HttpMethod) apitypes.WorkloadSpec {
	h := &apitypes.HttpSpec{Kind: apitypes.HttpKindEndpoint, Route: &route}
	if len(methods) > 0 {
		h.Methods = &methods
	}
	s := spec(name, handler, source, h)
	keepWarm, timeout := 2, 120
	s.KeepWarmSeconds, s.TimeoutSeconds = &keepWarm, &timeout
	return s
}

// An endpoint starts on its first request, answers on each of its URLs and
// only on its route and methods, and scales to zero once idle.
func TestEndpointColdWarmAndScaleToZero(t *testing.T) {
	t.Parallel()
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": endpointApp})
	d := p.deploy("api_demo", endpointSpec(source, "count_words", "app:count_words", "/word-count", apitypes.HttpMethodPOST))
	release := d.Releases[0].Id
	w := p.describe("api_demo", apitypes.WorkloadKindEndpoint, "count_words")
	if !strings.HasPrefix(w.Url, "http://count-words-") || !strings.HasSuffix(w.Url, "/word-count") {
		t.Fatalf("deployment URL %s", w.Url)
	}
	if status, _, body := p.call(http.MethodPost, w.Url, `{"text": "Run Python on LazyCloud"}`); status != http.StatusOK || body != `{"words": 4}` {
		t.Fatalf("cold request: %d %s", status, body)
	}
	// The pinned and release URLs reach the same release.
	for _, url := range []string{w.VersionUrl, w.ReleaseUrl} {
		if status, _, body := p.call(http.MethodPost, url, `{"text": "a b"}`); status != http.StatusOK || body != `{"words": 2}` {
			t.Fatalf("%s: %d %s", url, status, body)
		}
	}

	resp, err := p.request(http.MethodPost, w.Url, "", strings.NewReader(`{"text": "x"}`))
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("request without a token: %d", resp.StatusCode)
	}
	if status, _, _ := p.call(http.MethodPost, strings.TrimSuffix(w.Url, "/word-count")+"/other", `{}`); status != http.StatusNotFound {
		t.Fatalf("request off the route: %d", status)
	}
	if status, header, _ := p.call(http.MethodGet, w.Url, ""); status != http.StatusMethodNotAllowed || header.Get("Allow") != "POST" {
		t.Fatalf("GET of a POST endpoint: %d allow=%q", status, header.Get("Allow"))
	}

	// keep_warm is 2 s: without requests the release scales to zero.
	waitFor(t, "the idle endpoint to stop its container", func() bool { return liveContainers(t, p.pool, release) == 0 })
}

const startupApp = `
import os

started = []


def warm(context):
    started.append(os.environ["API_KEY"])


def whoami() -> dict:
    return {"key": os.environ["API_KEY"], "started": started}
`

// An endpoint's container carries the secrets it names, and each worker runs
// on_start before it takes a request.
func TestEndpointGetsItsSecretsAndRunsOnStartFirst(t *testing.T) {
	t.Parallel()
	p := startPlatform(t)
	if _, err := p.secrets.Set(t.Context(), p.workspace.ID, "API_KEY", "s3cret"); err != nil {
		t.Fatal(err)
	}
	source := p.upload(map[string]string{"app.py": startupApp})
	s := endpointSpec(source, "whoami", "app:whoami", "/")
	s.Secrets = &[]string{"API_KEY"}
	s.LifecycleHooks = &apitypes.LifecycleHooks{OnStart: &apitypes.HookReferences{"app:warm"}}
	p.deploy("startup", s)
	w := p.describe("startup", apitypes.WorkloadKindEndpoint, "whoami")
	status, _, body := p.call(http.MethodGet, w.Url, "")
	if status != http.StatusOK || body != `{"key": "s3cret", "started": ["s3cret"]}` {
		t.Fatalf("whoami: %d %s", status, body)
	}
}

const publicApp = `
def total(values: list[int]) -> int:
    return sum(values)


async def echo(scope, receive, send):
    if scope["type"] != "http":
        return
    auth = dict(scope["headers"]).get(b"authorization", b"")
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
    await send({"type": "http.response.body", "body": b"auth=" + auth})
`

// A public function or app answers without a token on every URL it has, and
// an app sees the caller's own Authorization header, which the platform did
// not consume.
func TestPublicWorkloadsAnswerWithoutAToken(t *testing.T) {
	t.Parallel()
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": publicApp})
	public := false
	fn := spec("total", "app:total", source, nil)
	fn.Authorized = &public
	app := spec("echo", "app:echo", source, &apitypes.HttpSpec{Kind: apitypes.HttpKindAsgi})
	app.Authorized = &public
	d := p.deploy("open", fn, app)

	fnHost := control.Subdomain(uuid.UUID(p.workspace.ID), "open", "total", apitypes.WorkloadKindFunction)
	for _, url := range []string{
		fmt.Sprintf("http://%s.lazycloud.localhost:%s/", fnHost, p.port()),
		fmt.Sprintf("http://%s-latest.lazycloud.localhost:%s/", fnHost, p.port()),
		fmt.Sprintf("http://%s.lazycloud.localhost:%s/", d.Releases[0].Id, p.port()),
	} {
		resp, err := p.request(http.MethodPost, url, "", strings.NewReader(`{"values": [1, 2, 3]}`))
		if err != nil {
			t.Fatal(err)
		}
		var out apitypes.Invocation
		err = json.NewDecoder(resp.Body).Decode(&out)
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusOK || err != nil || out.Result == nil || string(*out.Result) != "6" {
			t.Fatalf("public function at %s: %d %v %+v", url, resp.StatusCode, err, out)
		}
	}

	w := p.describe("open", apitypes.WorkloadKindAsgi, "echo")
	req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, w.Url, nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Basic dXNlcjpwdw==")
	resp, err := p.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK || string(body) != "auth=Basic dXNlcjpwdw==" {
		t.Fatalf("public app: %d %s", resp.StatusCode, body)
	}
	// A platform token never reaches a workload, public or not, among the
	// app's own Authorization values too.
	req.Header.Set("Authorization", "Bearer "+p.token)
	resp, err = p.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ = io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK || string(body) != "auth=" {
		t.Fatalf("public app with a platform token: %d %s", resp.StatusCode, body)
	}
	req.Header.Set("Authorization", "Basic dXNlcjpwdw==")
	req.Header.Add("Authorization", "Bearer "+p.token)
	resp, err = p.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ = io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK || string(body) != "auth=Basic dXNlcjpwdw==" {
		t.Fatalf("public app with its own and a platform credential: %d %s", resp.StatusCode, body)
	}

	// The function goes private: its old public version stays closed on
	// every host that names it.
	fn.Authorized = nil
	p.deploy("open", fn)
	// The route table follows the deploy's notification.
	waitFor(t, "the function to go private", func() bool {
		return p.anonymousStatus(fmt.Sprintf("http://%s.lazycloud.localhost:%s/", fnHost, p.port())) == http.StatusUnauthorized
	})
	for _, url := range []string{
		fmt.Sprintf("http://%s.lazycloud.localhost:%s/", fnHost, p.port()),
		fmt.Sprintf("http://%s-v1.lazycloud.localhost:%s/", fnHost, p.port()),
		fmt.Sprintf("http://%s.lazycloud.localhost:%s/", d.Releases[0].Id, p.port()),
	} {
		if status := p.anonymousStatus(url); status != http.StatusUnauthorized {
			t.Fatalf("old public version at %s after going private: %d; want 401", url, status)
		}
	}
}

// anonymousStatus is the status a POST to url without a token gets.
func (p *platform) anonymousStatus(url string) int {
	p.t.Helper()
	resp, err := p.request(http.MethodPost, url, "", strings.NewReader("{}"))
	if err != nil {
		p.t.Fatal(err)
	}
	_ = resp.Body.Close()
	return resp.StatusCode
}
