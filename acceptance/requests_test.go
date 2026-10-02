package acceptance

import (
	"fmt"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// Endpoint requests run as no task; each gets an X-Request-Id, a record
// with its status and latency, and keeps what the handler printed.
func TestEndpointRequestsAreRecordedWithTheirOutput(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": endpointApp})
	p.deploy("api_demo", endpointSpec(source, "count_words", "app:count_words", "/word-count", apitypes.HttpMethodPOST))
	w := p.describe("api_demo", apitypes.WorkloadKindEndpoint, "count_words")

	req, err := http.NewRequestWithContext(t.Context(), http.MethodPost, w.Url, strings.NewReader(`{"text": "one two three"}`))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+p.token)
	req.Header.Set("X-Request-Id", "made-up")
	resp, err := p.client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	id := resp.Header.Get("X-Request-Id")
	if resp.StatusCode != http.StatusOK || id == "" || id == "made-up" {
		t.Fatalf("response %d with X-Request-Id %q", resp.StatusCode, id)
	}

	var list apitypes.HttpRequestList
	deadline := time.Now().Add(10 * time.Second)
	for len(list.Data) == 0 {
		if time.Now().After(deadline) {
			t.Fatal("the request was never recorded")
		}
		time.Sleep(200 * time.Millisecond)
		if status := p.apiCall(http.MethodGet, "/v1/workspaces/ws/apps/api_demo/requests?name=count_words", nil, &list); status != http.StatusOK {
			t.Fatalf("list requests: %d", status)
		}
	}
	got := list.Data[0]
	if got.Id.String() != id || got.Status != http.StatusOK || got.Method != http.MethodPost || got.Path != "/word-count" ||
		got.ContainerId == nil || got.RequestBytes == 0 || got.ResponseBytes == 0 || got.Kind != apitypes.WorkloadKindEndpoint {
		t.Fatalf("record %+v", got)
	}

	var logs apitypes.ContainerLogList
	deadline = time.Now().Add(10 * time.Second)
	for {
		if status := p.apiCall(http.MethodGet, fmt.Sprintf("/v1/workspaces/ws/requests/%s/logs", id), nil, &logs); status != http.StatusOK {
			t.Fatalf("request logs: %d", status)
		}
		if len(logs.Data) > 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the request's output never arrived")
		}
		time.Sleep(200 * time.Millisecond)
	}
	if len(logs.Data) != 1 || logs.Data[0].Data != "counting one two three" {
		t.Fatalf("request logs %+v", logs.Data)
	}
}
