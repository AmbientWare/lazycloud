package acceptance

import (
	"net/http"
	"slices"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
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
