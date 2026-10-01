package acceptance

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"slices"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/control"
)

const endpointApp = `
import os

import lazycloud
from pydantic import BaseModel

app = lazycloud.App("api_demo")


class WordCount(BaseModel):
    words: int


@app.endpoint(name="count_words", route="/word-count", methods=["POST"])
def count_words(text: str) -> WordCount:
    print("counting", text, flush=True)
    return WordCount(words=len(text.split()))


@app.endpoint(name="headers")
def headers(name: str = ""):
    return {"ok": True}
`

func endpointSpec(source, name, handler, route string, methods ...apitypes.HttpMethod) apitypes.FunctionSpec {
	h := &apitypes.HttpSpec{Kind: apitypes.HttpKindEndpoint, Route: &route}
	if len(methods) > 0 {
		h.Methods = &methods
	}
	s := spec(name, handler, source, h)
	keepWarm, timeout := 2, 120
	s.KeepWarmSeconds, s.TimeoutSeconds = &keepWarm, &timeout
	return s
}

func percentile(samples []time.Duration, p float64) time.Duration {
	sorted := slices.Clone(samples)
	sort.Slice(sorted, func(a, b int) bool { return sorted[a] < sorted[b] })
	return sorted[int(float64(len(sorted)-1)*p)]
}

func TestEndpointColdWarmAndScaleToZero(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": endpointApp})
	d := p.deploy("api_demo", endpointSpec(source, "count_words", "app:count_words", "/word-count", apitypes.HttpMethodPOST))
	release := d.Releases[0].Id
	w := p.describe("api_demo", apitypes.WorkloadKindEndpoint, "count_words")
	if !strings.HasPrefix(w.Url, "http://count-words-") || !strings.HasSuffix(w.Url, "/word-count") {
		t.Fatalf("deployment URL %s", w.Url)
	}

	started := time.Now()
	status, _, body := p.call(http.MethodPost, w.Url, `{"text": "Run Python on LazyCloud"}`)
	cold := time.Since(started)
	if status != http.StatusOK || body != `{"words": 4}` {
		t.Fatalf("cold request: %d %s", status, body)
	}
	t.Logf("cold request through the edge (planning, placement, container start, handler import): %s", cold)

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

	var warm []time.Duration
	for range 200 {
		began := time.Now()
		status, _, _ := p.call(http.MethodPost, w.Url, `{"text": "one two"}`)
		warm = append(warm, time.Since(began))
		if status != http.StatusOK {
			t.Fatalf("warm request: %d", status)
		}
	}
	t.Logf("warm request through the edge over 200: p50 %s, p95 %s", percentile(warm, 0.5), percentile(warm, 0.95))

	// keep_warm is 2 s: without requests the release scales to zero.
	idleFrom := time.Now()
	deadline := time.Now().Add(30 * time.Second)
	for liveContainers(t, p.pool, release) > 0 {
		if time.Now().After(deadline) {
			t.Fatal("the idle endpoint kept its container")
		}
		time.Sleep(100 * time.Millisecond)
	}
	t.Logf("last request to no live container: %s (keep_warm 2s)", time.Since(idleFrom))
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
	// A platform token never reaches a workload, public or not.
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

	// The function goes private: its old public version stays closed on
	// every host that names it.
	fn.Authorized = nil
	p.deploy("open", fn)
	// The route table follows the deploy's notification.
	p.waitForStatus(http.MethodPost, fmt.Sprintf("http://%s.lazycloud.localhost:%s/", fnHost, p.port()), http.StatusUnauthorized)
	for _, url := range []string{
		fmt.Sprintf("http://%s.lazycloud.localhost:%s/", fnHost, p.port()),
		fmt.Sprintf("http://%s-v1.lazycloud.localhost:%s/", fnHost, p.port()),
		fmt.Sprintf("http://%s.lazycloud.localhost:%s/", d.Releases[0].Id, p.port()),
	} {
		resp, err := p.request(http.MethodPost, url, "", strings.NewReader(`{"values": [1]}`))
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode != http.StatusUnauthorized {
			t.Fatalf("old public version at %s after going private: %d; want 401", url, resp.StatusCode)
		}
	}
}

const volumeApp = `
from pathlib import Path


def hits() -> int:
    path = Path("/volumes/data/hits")
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    return count
`

// An endpoint mounts its volumes like a function: what one request writes
// the next reads, in a redeployed container too.
func TestEndpointMountsItsVolumes(t *testing.T) {
	p := startPlatform(t)
	if p.geesefs == "" {
		t.Skip("volume mounts need GeeseFS; run deploy/local/fetch-geesefs.sh")
	}
	source := p.upload(map[string]string{"app.py": volumeApp})
	s := endpointSpec(source, "hits", "app:hits", "/")
	s.Volumes = &[]apitypes.VolumeMountSpec{{Name: "data"}}
	p.deploy("volumes", s)
	w := p.describe("volumes", apitypes.WorkloadKindEndpoint, "hits")
	for want := 1; want <= 2; want++ {
		if status, _, body := p.call(http.MethodGet, w.Url, ""); status != http.StatusOK || body != fmt.Sprint(want) {
			t.Fatalf("request %d: %d %s", want, status, body)
		}
	}
	// A new release runs in a new container on the same volume.
	timeout := 90
	s.TimeoutSeconds = &timeout
	p.deploy("volumes", s)
	if status, _, body := p.call(http.MethodGet, w.Url, ""); status != http.StatusOK || body != "3" {
		t.Fatalf("after redeploy: %d %s", status, body)
	}
}

// waitForStatus fails unless a request without a token gets status within
// two seconds.
func (p *platform) waitForStatus(method, url string, status int) {
	p.t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for {
		resp, err := p.request(method, url, "", strings.NewReader("{}"))
		if err != nil {
			p.t.Fatal(err)
		}
		_ = resp.Body.Close()
		if resp.StatusCode == status {
			return
		}
		if time.Now().After(deadline) {
			p.t.Fatalf("%s %s: %d; want %d", method, url, resp.StatusCode, status)
		}
		time.Sleep(20 * time.Millisecond)
	}
}
