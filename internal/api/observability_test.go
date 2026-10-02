package api_test

import (
	"bufio"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// sseEvent is one parsed text/event-stream event.
type sseEvent struct {
	id, event, data string
}

// openStream opens the workspace change stream; lastEventID may be empty.
// It returns the response head; the body closes when the test ends.
func (e *env) openStream(token, lastEventID string) (streamHead, <-chan sseEvent) {
	e.t.Helper()
	req, err := http.NewRequestWithContext(e.t.Context(), "GET", e.url+"/v1/workspaces/acme/changes/stream", nil)
	if err != nil {
		e.t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+token)
	if lastEventID != "" {
		req.Header.Set("Last-Event-ID", lastEventID)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	e.t.Cleanup(func() { _ = resp.Body.Close() })
	head := streamHead{status: resp.StatusCode, contentType: resp.Header.Get("Content-Type")}
	events := make(chan sseEvent, 64)
	if resp.StatusCode != http.StatusOK {
		close(events)
		return head, events
	}
	go func() {
		defer close(events)
		scanner := bufio.NewScanner(resp.Body)
		var ev sseEvent
		for scanner.Scan() {
			line := scanner.Text()
			switch {
			case line == "":
				if ev.event != "" {
					events <- ev
				}
				ev = sseEvent{}
			case strings.HasPrefix(line, "id: "):
				ev.id = strings.TrimPrefix(line, "id: ")
			case strings.HasPrefix(line, "event: "):
				ev.event = strings.TrimPrefix(line, "event: ")
			case strings.HasPrefix(line, "data: "):
				ev.data = strings.TrimPrefix(line, "data: ")
			}
		}
	}()
	return head, events
}

type streamHead struct {
	status      int
	contentType string
}

// nextChange returns the next change event that has a change of topic.
func (e *env) nextChange(events <-chan sseEvent, topic apitypes.ChangeTopic) (sseEvent, apitypes.ChangeEvent) {
	e.t.Helper()
	deadline := time.After(10 * time.Second)
	for {
		select {
		case ev, ok := <-events:
			if !ok {
				e.t.Fatal("the stream ended")
			}
			if ev.event != "change" {
				e.t.Fatalf("unexpected %s event: %s", ev.event, ev.data)
			}
			var change apitypes.ChangeEvent
			if err := json.Unmarshal([]byte(ev.data), &change); err != nil {
				e.t.Fatal(err)
			}
			for _, c := range change.Changes {
				if c.Topic == topic {
					return ev, change
				}
			}
		case <-deadline:
			e.t.Fatalf("no %s change", topic)
		}
	}
}

// The change stream is authorized like every workspace operation, carries
// the workspace's committed changes as server-sent events, and resumes after
// Last-Event-ID.
func TestChangeStreamOverHTTP(t *testing.T) {
	e := newEnv(t)
	if head, _ := e.openStream(e.outsider, ""); head.status != http.StatusForbidden {
		t.Fatalf("outsider stream: %d", head.status)
	}
	if head, _ := e.openStream(e.owner, "not-an-id"); head.status != http.StatusBadRequest {
		t.Fatalf("malformed Last-Event-ID: %d", head.status)
	}
	head, events := e.openStream(e.owner, "")
	if head.contentType != "text/event-stream" {
		t.Fatalf("content type %q", head.contentType)
	}
	e.deploy()
	e.nextChange(events, apitypes.ChangeTopicDeployments)

	fnPath := "/v1/workspaces/acme/apps/reports/workloads/function/summarize_sales/tasks"
	raw := json.RawMessage(`{"args": [], "kwargs": {}}`)
	submit := apitypes.SubmitTasksRequest{Inputs: []apitypes.TaskInput{{Encoding: apitypes.TaskInputEncodingJson, Value: &raw}}}
	var first, second apitypes.SubmitTasksResponse
	if status := e.do("POST", fnPath, e.owner, submit, &first); status != 201 {
		t.Fatalf("submit: %d", status)
	}
	created, change := e.nextChange(events, apitypes.ChangeTopicTasks)
	if *change.Changes[0].TaskId != first.Tasks[0].Id || created.id == "" {
		t.Fatalf("task change %+v id %q", change, created.id)
	}
	if status := e.do("POST", fnPath, e.owner, submit, &second); status != 201 {
		t.Fatalf("submit: %d", status)
	}
	e.nextChange(events, apitypes.ChangeTopicTasks)

	_, resumed := e.openStream(e.owner, created.id)
	_, replayed := e.nextChange(resumed, apitypes.ChangeTopicTasks)
	if *replayed.Changes[0].TaskId != second.Tasks[0].Id {
		t.Fatalf("resumed with %+v, want the second task", replayed)
	}
	_, reset := e.openStream(e.owner, "999999999")
	select {
	case ev := <-reset:
		if ev.event != "reset" || !strings.Contains(ev.data, `"unknown_cursor"`) {
			t.Fatalf("stale cursor got %+v", ev)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("no reset for a stale cursor")
	}
}

// Every observability read authorizes the workspace, validates its range
// and reports unknown resources as not found.
func TestObservabilityRoutesAuthorizeAndValidate(t *testing.T) {
	e := newEnv(t)
	e.deploy()
	unknown := uuid.NewString()
	workload := "/v1/workspaces/acme/apps/reports/workloads/function/summarize_sales"
	var apiErr apitypes.Error
	for _, path := range []string{
		"/v1/workspaces/acme/metrics/tasks",
		"/v1/workspaces/acme/metrics/activity",
		workload + "/performance",
		"/v1/workspaces/acme/containers/" + unknown + "/metrics",
		"/v1/workspaces/acme/tasks/" + unknown + "/timeline",
	} {
		if status := e.do("GET", path, e.outsider, nil, &apiErr); status != http.StatusForbidden {
			t.Fatalf("outsider %s: %d", path, status)
		}
	}
	for path, want := range map[string]int{
		"/v1/workspaces/acme/metrics/tasks":                                                        200,
		"/v1/workspaces/acme/metrics/activity?window_seconds=900":                                  200,
		workload + "/performance":                                                                  200,
		"/v1/workspaces/acme/metrics/activity?window_seconds=30":                                   400,
		"/v1/workspaces/acme/metrics/tasks?function=summarize_sales":                               400,
		"/v1/workspaces/acme/metrics/activity?window_seconds=60&start=2026-01-01T00:00:00Z":        400,
		"/v1/workspaces/acme/containers/" + unknown + "/metrics":                                   404,
		"/v1/workspaces/acme/containers/" + unknown + "/lifecycle":                                 404,
		"/v1/workspaces/acme/tasks/" + unknown + "/timeline":                                       404,
		"/v1/workspaces/acme/tasks/" + unknown + "/call-graph":                                     404,
		"/v1/workspaces/acme/apps/reports/workloads/function/unknown/performance":                  404,
		"/v1/workspaces/acme/metrics/tasks?start=2026-01-01T00:00:00Z&end=2026-03-01T00:00:00Z":    400,
		"/v1/workspaces/acme/metrics/activity?window_seconds=604800":                               200,
		"/v1/me/activity?window_seconds=86400&start=2026-01-01T00:00:00Z&end=2026-03-01T00:00:00Z": 400,
		"/v1/me/metrics":               200,
		"/v1/me/activity?measure=cpu":  200,
		"/v1/me/activity?measure=disk": 400,
	} {
		if status := e.do("GET", path, e.owner, nil, nil); status != want {
			t.Fatalf("%s: %d, want %d", path, status, want)
		}
	}
	var lifecycles apitypes.ContainerLifecycleList
	if status := e.do("POST", "/v1/workspaces/acme/containers/lifecycles", e.owner,
		apitypes.ContainerLifecyclesRequest{ContainerIds: []uuid.UUID{uuid.New()}}, &lifecycles); status != 200 || len(lifecycles.Lifecycles) != 0 {
		t.Fatalf("lifecycles of unknown containers: %d %+v", status, lifecycles)
	}
}
