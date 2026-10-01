package acceptance

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/control"
)

const capacityApp = `
import time

import lazycloud

app = lazycloud.App("load")


@app.endpoint(name="sleepy")
def sleepy(seconds: float = 0.0, version: str = "v1"):
    time.sleep(seconds)
    return {"version": version}
`

func capacitySpec(source string, workers, concurrency, maxContainers, maxPending int) apitypes.WorkloadSpec {
	route := "/"
	s := spec("sleepy", "app:sleepy", source, &apitypes.HttpSpec{Kind: apitypes.HttpKindEndpoint, Route: &route, Workers: &workers})
	keepWarm, timeout, tpc := 30, 120, workers*concurrency
	s.Concurrency, s.KeepWarmSeconds, s.TimeoutSeconds, s.MaxPendingTasks = &concurrency, &keepWarm, &timeout, &maxPending
	s.Autoscaler = &apitypes.Autoscaler{MaxContainers: &maxContainers, TasksPerContainer: &tpc}
	return s
}

// concurrently sends n requests at once and returns their statuses and
// latencies.
func (p *platform) concurrently(n int, url string) ([]int, []time.Duration, time.Duration) {
	statuses := make([]int, n)
	latencies := make([]time.Duration, n)
	var wg sync.WaitGroup
	began := time.Now()
	for i := range n {
		wg.Go(func() {
			start := time.Now()
			resp, err := p.request(http.MethodGet, url, p.token, nil)
			if err != nil {
				p.t.Errorf("request %d: %v", i, err)
				return
			}
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			statuses[i], latencies[i] = resp.StatusCode, time.Since(start)
		})
	}
	wg.Wait()
	return statuses, latencies, time.Since(began)
}

func count(statuses []int, status int) int {
	n := 0
	for _, s := range statuses {
		if s == status {
			n++
		}
	}
	return n
}

func TestEndpointQueuesPastCapacityAndRejectsPastMaxPending(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": capacityApp})
	// One container of one worker and one slot; five may wait.
	p.deploy("load", capacitySpec(source, 1, 1, 1, 5))
	w := p.describe("load", apitypes.WorkloadKindEndpoint, "sleepy")
	if status, _, _ := p.call(http.MethodGet, w.Url, ""); status != http.StatusOK {
		t.Fatalf("warm-up: %d", status)
	}
	statuses, _, _ := p.concurrently(12, w.Url+"?seconds=0.5")
	if ok, rejected := count(statuses, http.StatusOK), count(statuses, http.StatusTooManyRequests); ok < 6 || rejected == 0 || ok+rejected != 12 {
		t.Fatalf("12 requests with capacity 1 and max_pending 5: %d ok, %d rejected: %v", ok, rejected, statuses)
	}
}

func TestEndpointServesAThousandConcurrentRequests(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": capacityApp})
	// Two containers of two workers of 16 requests: 64 at once.
	p.deploy("load", capacitySpec(source, 2, 16, 2, 1000))
	w := p.describe("load", apitypes.WorkloadKindEndpoint, "sleepy")
	if status, _, _ := p.call(http.MethodGet, w.Url, ""); status != http.StatusOK {
		t.Fatalf("warm-up: %d", status)
	}
	statuses, latencies, total := p.concurrently(1000, w.Url+"?seconds=0.01")
	if ok := count(statuses, http.StatusOK); ok != 1000 {
		t.Fatalf("%d of 1000 succeeded: %v", ok, statuses)
	}
	t.Logf("1000 concurrent requests of 10 ms on up to 2 containers of capacity 32: %s total, p50 %s, p95 %s",
		total, percentile(latencies, 0.5), percentile(latencies, 0.95))
}

func TestRedeployKeepsServingUntilTheNewReleaseIsReadyThenDrains(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": capacityApp})
	first := p.deploy("load", capacitySpec(source, 1, 4, 1, 100))
	w := p.describe("load", apitypes.WorkloadKindEndpoint, "sleepy")
	if status, _, body := p.call(http.MethodGet, w.Url, ""); status != http.StatusOK || !strings.Contains(body, "v1") {
		t.Fatalf("v1: %d %s", status, body)
	}
	edited := p.upload(map[string]string{"app.py": strings.Replace(capacityApp, `version: str = "v1"`, `version: str = "v2"`, 1)})
	p.deploy("load", capacitySpec(edited, 1, 4, 1, 100))

	// Every request succeeds across the switch: v1 serves until v2 is ready.
	deadline := time.Now().Add(60 * time.Second)
	sawV2 := false
	for !sawV2 {
		if time.Now().After(deadline) {
			t.Fatal("v2 never answered")
		}
		status, _, body := p.call(http.MethodGet, w.Url, "")
		if status != http.StatusOK {
			t.Fatalf("request during the redeploy: %d %s", status, body)
		}
		sawV2 = strings.Contains(body, "v2")
		time.Sleep(20 * time.Millisecond)
	}
	for liveContainers(t, p.pool, first.Releases[0].Id) > 0 {
		if time.Now().After(deadline) {
			t.Fatal("the replaced release kept its container")
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func TestEndpointThatFailsToLoadAnswersWithItsError(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": capacityApp + "\nraise RuntimeError('no database configured')\n"})
	p.deploy("load", capacitySpec(source, 1, 1, 1, 100))
	w := p.describe("load", apitypes.WorkloadKindEndpoint, "sleepy")
	started := time.Now()
	status, _, body := p.call(http.MethodGet, w.Url, "")
	if status != http.StatusInternalServerError || !strings.Contains(body, "no database configured") {
		t.Fatalf("request to a release that cannot load: %d %s", status, body)
	}
	t.Logf("load error reported to the caller after %s", time.Since(started))
}

func TestFunctionsAreInvokedOverHTTP(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": `
import lazycloud

app = lazycloud.App("reports")


@app.function()
def total(values: list[int], scale: float = 1.0) -> float:
    return sum(values) * scale
`})
	p.deploy("reports", spec("total", "app:total", source, nil))
	url := fmt.Sprintf("http://%s.lazycloud.localhost:%s/?scale=2", control.Subdomain(uuid.UUID(p.workspace.ID), "reports", "total", apitypes.WorkloadKindFunction), p.port())
	status, header, body := p.call(http.MethodPost, url, `{"values": [1200, 3500, 800]}`)
	var out apitypes.Invocation
	if status != http.StatusOK || json.Unmarshal([]byte(body), &out) != nil || out.Task.Status != apitypes.TaskStatusSucceeded || out.Result == nil || string(*out.Result) != "11000.0" {
		t.Fatalf("invoke over HTTP: %d %s", status, body)
	}
	if header.Get("X-Task-Id") != out.Task.Id.String() {
		t.Fatalf("X-Task-Id %q; want the task %s", header.Get("X-Task-Id"), out.Task.Id)
	}
	if status, _, _ := p.call(http.MethodGet, url, ""); status != http.StatusMethodNotAllowed {
		t.Fatalf("GET of a function: %d", status)
	}
}

// TestWarmLatencyThroughTheEdgeAndDirect compares a warm request through the
// edge, data connection, agent and supervisor with one straight to the
// supervisor's socket on the host.
func TestWarmLatencyThroughTheEdgeAndDirect(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": capacityApp})
	d := p.deploy("load", capacitySpec(source, 1, 1, 1, 100))
	w := p.describe("load", apitypes.WorkloadKindEndpoint, "sleepy")
	if status, _, _ := p.call(http.MethodGet, w.Url, ""); status != http.StatusOK {
		t.Fatal("warm-up failed")
	}
	var container uuid.UUID
	if err := p.pool.QueryRow(t.Context(), `select id from containers where release_id = $1 and state = 'ready'`, d.Releases[0].Id).Scan(&container); err != nil {
		t.Fatal(err)
	}
	socket := filepath.Join(p.socketDir, container.String(), "http.sock")
	if _, err := os.Stat(socket); err != nil {
		t.Fatal(err)
	}
	direct := &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", socket)
	}}}
	measure := func(do func() int) []time.Duration {
		samples := make([]time.Duration, 0, 500)
		for range 500 {
			began := time.Now()
			if status := do(); status != http.StatusOK {
				t.Fatalf("status %d", status)
			}
			samples = append(samples, time.Since(began))
		}
		return samples
	}
	edge := measure(func() int { status, _, _ := p.call(http.MethodGet, w.Url, ""); return status })
	straight := measure(func() int {
		req, _ := http.NewRequestWithContext(t.Context(), http.MethodGet, "http://container/", nil)
		resp, err := direct.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = io.Copy(io.Discard, resp.Body)
		_ = resp.Body.Close()
		return resp.StatusCode
	})
	t.Logf("warm, 500 sequential: edge p50 %s p95 %s; supervisor socket direct p50 %s p95 %s",
		percentile(edge, 0.5), percentile(edge, 0.95), percentile(straight, 0.5), percentile(straight, 0.95))
}
