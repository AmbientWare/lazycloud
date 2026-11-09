package api_test

import (
	"testing"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// A webhook belongs to its workspace: another workspace's members reach it
// neither through that workspace nor through their own.
func TestWebhooksStayInTheirWorkspace(t *testing.T) {
	t.Parallel()
	e := newEnv(t)
	if _, err := e.identity.CreateWorkspace(t.Context(), "other", "outsider@example.com"); err != nil {
		t.Fatal(err)
	}
	var hook apitypes.Webhook
	if status := e.do("POST", "/v1/workspaces/acme/webhooks", e.owner, map[string]any{
		"url": "https://hooks.example.com/lazycloud", "apps": []string{"reports", "billing"}, "events": []string{"task.failed"},
	}, &hook); status != 201 || len(hook.Apps) != 2 || hook.Apps[0] != "reports" || !hook.Enabled {
		t.Fatalf("create: %d %+v", status, hook)
	}
	var updated apitypes.Webhook
	if status := e.do("PATCH", "/v1/workspaces/acme/webhooks/"+hook.Id.String(), e.owner,
		map[string]any{"apps": []string{}, "enabled": false}, &updated); status != 200 || len(updated.Apps) != 0 || updated.Enabled {
		t.Fatalf("update: %d %+v", status, updated)
	}
	var apiErr apitypes.Error
	if status := e.do("GET", "/v1/workspaces/acme/webhooks/"+hook.Id.String()+"/deliveries", e.outsider, nil, &apiErr); status != 403 {
		t.Fatalf("outsider through acme: %d %+v", status, apiErr)
	}
	for method, suffix := range map[string]string{"GET": "/deliveries", "DELETE": ""} {
		if status := e.do(method, "/v1/workspaces/other/webhooks/"+hook.Id.String()+suffix, e.outsider, nil, &apiErr); status != 404 {
			t.Fatalf("%s through another workspace: %d %+v", method, status, apiErr)
		}
	}
	var delivery apitypes.WebhookDelivery
	if status := e.do("POST", "/v1/workspaces/acme/webhooks/"+hook.Id.String()+"/test", e.owner, nil, &delivery); status != 202 ||
		delivery.Event != "webhook.test" || delivery.State != apitypes.WebhookDeliveryPending {
		t.Fatalf("test: %d %+v", status, delivery)
	}
	if status := e.do("POST", "/v1/workspaces/acme/webhooks", e.owner, map[string]any{
		"url": "ftp://hooks.example.com", "events": []string{"task.failed"},
	}, &apiErr); status != 400 || apiErr.Code != apitypes.InvalidRequest {
		t.Fatalf("ftp url: %d %+v", status, apiErr)
	}
}
