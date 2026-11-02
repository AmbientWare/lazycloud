package callbacks

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// An event reaches each enabled webhook of its workspace that takes its type
// and either names its app among others or names none, signed with the workspace signing key;
// each webhook records its delivery.
func TestWebhookEventsReachTheWebhooksThatTakeThem(t *testing.T) {
	t.Parallel()
	ctx := t.Context()
	pool := dbtest.New(t)
	vault := newSecrets(t, pool)
	srv := &target{statuses: []int{http.StatusBadGateway}}
	server := httptest.NewServer(srv)
	defer server.Close()
	var ws, other uuid.UUID
	if err := pool.QueryRow(ctx, "insert into workspaces (name) values ('ws') returning id").Scan(&ws); err != nil {
		t.Fatal(err)
	}
	if err := pool.QueryRow(ctx, "insert into workspaces (name) values ('other') returning id").Scan(&other); err != nil {
		t.Fatal(err)
	}
	hooks := NewWebhooks(pool)
	create := func(workspace uuid.UUID, path string, apps []string, enabled bool, events ...apitypes.WebhookEvent) apitypes.Webhook {
		t.Helper()
		hook, err := hooks.Create(ctx, identity.WorkspaceID(workspace), apitypes.WebhookCreate{
			Url: server.URL + path, Apps: &apps, Events: events, Enabled: &enabled,
		})
		if err != nil {
			t.Fatal(err)
		}
		return hook
	}
	every := create(ws, "/every", nil, true, apitypes.WebhookTaskFailed)
	scoped := create(ws, "/billing", []string{"reports", "billing"}, true, apitypes.WebhookTaskFailed, apitypes.WebhookWorkloadDeployed)
	create(ws, "/reports", []string{"reports"}, true, apitypes.WebhookTaskFailed)
	create(ws, "/deploys", nil, true, apitypes.WebhookWorkloadDeployed)
	create(ws, "/disabled", nil, false, apitypes.WebhookTaskFailed)
	create(other, "/other", nil, true, apitypes.WebhookTaskFailed)

	var written int
	if err := pool.QueryRow(ctx, "select lc_webhook_event($1, 'billing', 'task.failed', '{\"task_id\": \"t1\"}')", ws).Scan(&written); err != nil {
		t.Fatal(err)
	}
	if written != 2 {
		t.Fatalf("the event reached %d webhooks, want the workspace-wide and the billing one", written)
	}

	c := newCallbacks(t, pool, vault, true)
	deadline := time.Now().Add(time.Minute)
	for len(srv.requests()) < 3 {
		if _, err := c.Deliver(ctx); err != nil {
			t.Fatal(err)
		}
		if time.Now().After(deadline) {
			t.Fatalf("%d requests arrived, want 2 deliveries and 1 retry", len(srv.requests()))
		}
		if next, ok, err := c.NextDue(ctx); err != nil {
			t.Fatal(err)
		} else if ok {
			time.Sleep(time.Until(next))
		}
	}

	key, err := vault.SigningKey(ctx, identity.WorkspaceID(ws))
	if err != nil {
		t.Fatal(err)
	}
	var eventID string
	for _, got := range srv.requests() {
		if want := Sign(key, got.body, got.header.Get("X-Webhook-Timestamp")); got.header.Get("X-Webhook-Signature") != want {
			t.Fatal("the signature does not verify with the workspace signing key")
		}
		var body struct {
			ID        string         `json:"id"`
			Type      string         `json:"type"`
			Workspace string         `json:"workspace"`
			Data      map[string]any `json:"data"`
		}
		if err := json.Unmarshal(got.body, &body); err != nil {
			t.Fatal(err)
		}
		if body.Type != "task.failed" || got.header.Get("X-Webhook-Event") != body.Type || body.Workspace != "ws" || body.Data["task_id"] != "t1" || got.header.Get("X-Webhook-ID") != body.ID {
			t.Fatalf("delivered %s with headers %v", got.body, got.header)
		}
		if eventID != "" && body.ID != eventID {
			t.Fatal("the webhooks of one event received different event ids")
		}
		eventID = body.ID
	}

	for _, hook := range []apitypes.Webhook{every, scoped} {
		page, err := hooks.Deliveries(ctx, identity.WorkspaceID(ws), hook.Id, "", 10)
		if err != nil {
			t.Fatal(err)
		}
		if len(page.Deliveries) != 1 || page.Deliveries[0].State != apitypes.WebhookDeliveryDelivered {
			t.Fatalf("webhook %s deliveries %+v", hook.Url, page.Deliveries)
		}
	}
}
