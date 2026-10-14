//go:build hostruntime

package acceptance

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"

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

// concurrently sends n requests at once and returns their statuses.
func (p *platform) concurrently(n int, url string) []int {
	statuses := make([]int, n)
	var wg sync.WaitGroup
	for i := range n {
		wg.Go(func() {
			resp, err := p.request(http.MethodGet, url, p.token, nil)
			if err != nil {
				p.t.Errorf("request %d: %v", i, err)
				return
			}
			_, _ = io.Copy(io.Discard, resp.Body)
			_ = resp.Body.Close()
			statuses[i] = resp.StatusCode
		})
	}
	wg.Wait()
	return statuses
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
	t.Parallel()
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": capacityApp})
	// One container of one worker and one slot; five may wait.
	p.deploy("load", capacitySpec(source, 1, 1, 1, 5))
	w := p.describe("load", apitypes.WorkloadKindEndpoint, "sleepy")
	if status, _, _ := p.call(http.MethodGet, w.Url, ""); status != http.StatusOK {
		t.Fatalf("warm-up: %d", status)
	}
	statuses := p.concurrently(12, w.Url+"?seconds=0.5")
	if ok, rejected := count(statuses, http.StatusOK), count(statuses, http.StatusTooManyRequests); ok < 6 || rejected == 0 || ok+rejected != 12 {
		t.Fatalf("12 requests with capacity 1 and max_pending 5: %d ok, %d rejected: %v", ok, rejected, statuses)
	}
}

func TestEndpointServesAThousandConcurrentRequests(t *testing.T) {
	t.Parallel()
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": capacityApp})
	// Two containers of two workers of 16 requests: 64 at once.
	p.deploy("load", capacitySpec(source, 2, 16, 2, 1000))
	w := p.describe("load", apitypes.WorkloadKindEndpoint, "sleepy")
	if status, _, _ := p.call(http.MethodGet, w.Url, ""); status != http.StatusOK {
		t.Fatalf("warm-up: %d", status)
	}
	statuses := p.concurrently(1000, w.Url+"?seconds=0.01")
	if ok := count(statuses, http.StatusOK); ok != 1000 {
		t.Fatalf("%d of 1000 succeeded: %v", ok, statuses)
	}
}

func TestRedeployKeepsServingUntilTheNewReleaseIsReadyThenDrains(t *testing.T) {
	t.Parallel()
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
	waitFor(t, "v2 to answer", func() bool {
		status, _, body := p.call(http.MethodGet, w.Url, "")
		if status != http.StatusOK {
			t.Fatalf("request during the redeploy: %d %s", status, body)
		}
		return strings.Contains(body, "v2")
	})
	waitFor(t, "the replaced release to stop its container", func() bool { return liveContainers(t, p.pool, first.Releases[0].Id) == 0 })
}

func TestEndpointThatFailsToLoadAnswersWithItsError(t *testing.T) {
	t.Parallel()
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": capacityApp + "\nraise RuntimeError('no database configured')\n"})
	p.deploy("load", capacitySpec(source, 1, 1, 1, 100))
	w := p.describe("load", apitypes.WorkloadKindEndpoint, "sleepy")
	status, _, body := p.call(http.MethodGet, w.Url, "")
	if status != http.StatusInternalServerError || !strings.Contains(body, "no database configured") {
		t.Fatalf("request to a release that cannot load: %d %s", status, body)
	}
}

func TestFunctionsAreInvokedOverHTTP(t *testing.T) {
	t.Parallel()
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
