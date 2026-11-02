//go:build hostruntime

package acceptance

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/callbacks"
	"github.com/AmbientWare/lazycloud/internal/control"
)

// A webhook receives the deploy of a function and the failure of its task as
// signed requests, from a real run on a host.
func TestWebhookReceivesADeployAndAFailedTask(t *testing.T) {
	t.Parallel()
	p := startPlatform(t)
	type received struct {
		header http.Header
		body   []byte
	}
	arrivals := make(chan received, 16)
	receiver := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		arrivals <- received{header: r.Header.Clone(), body: body}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer receiver.Close()
	if status := p.apiCall(http.MethodPost, "/v1/workspaces/"+p.workspace.Name+"/webhooks", map[string]any{
		"url": receiver.URL, "app": "reports", "events": []string{"workload.deployed", "task.failed"},
	}, nil); status != http.StatusCreated {
		t.Fatalf("create webhook: %d", status)
	}

	source := p.upload(map[string]string{"app.py": `
import lazycloud

app = lazycloud.App("reports")


@app.function(retries=0)
def total(values: list[int]) -> int:
    raise ValueError("no totals today")
`})
	p.deploy("reports", spec("total", "app:total", source, nil))
	url := fmt.Sprintf("http://%s.lazycloud.localhost:%s/", control.Subdomain(uuid.UUID(p.workspace.ID), "reports", "total", apitypes.WorkloadKindFunction), p.port())
	p.call(http.MethodPost, url, `{"values": [1, 2]}`)

	key, err := p.secrets.SigningKey(t.Context(), p.workspace.ID)
	if err != nil {
		t.Fatal(err)
	}
	events := map[string]map[string]any{}
	deadline := time.After(5 * time.Minute)
	for len(events) < 2 {
		var r received
		select {
		case r = <-arrivals:
		case <-deadline:
			t.Fatalf("received %v, want workload.deployed and task.failed", events)
		}
		if want := callbacks.Sign(key, r.body, r.header.Get("X-Webhook-Timestamp")); r.header.Get("X-Webhook-Signature") != want {
			t.Fatalf("%s: the signature does not verify", r.header.Get("X-Webhook-Event"))
		}
		var body struct {
			Type string         `json:"type"`
			Data map[string]any `json:"data"`
		}
		if err := json.Unmarshal(r.body, &body); err != nil {
			t.Fatal(err)
		}
		events[body.Type] = body.Data
	}
	failure, _ := events["task.failed"]["error"].(map[string]any)
	if events["workload.deployed"]["app"] != "reports" || failure["message"] == nil {
		t.Fatalf("received %v", events)
	}
}
