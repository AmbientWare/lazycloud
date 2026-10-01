package api_test

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// The control routes share the public API's authorization, schema
// validation and typed errors. Owner behavior is proven in the control and
// execution packages; this covers what only the HTTP surface adds.
func TestControlRoutesAuthorizeValidateAndRoute(t *testing.T) {
	e := newEnv(t)
	e.deploy()
	var apiErr apitypes.Error
	for _, path := range []string{
		"/v1/workspaces/acme/apps",
		"/v1/workspaces/acme/deployments",
		"/v1/workspaces/acme/tasks",
		"/v1/workspaces/acme/containers",
	} {
		if status := e.do("GET", path, e.outsider, nil, &apiErr); status != http.StatusForbidden {
			t.Fatalf("outsider %s: %d %+v", path, status, apiErr)
		}
	}
	if status := e.do("POST", "/v1/workspaces/acme/tasks/stop", e.outsider,
		apitypes.StopTasksRequest{TaskIds: []uuid.UUID{uuid.New()}}, &apiErr); status != http.StatusForbidden {
		t.Fatalf("outsider stop: %d", status)
	}

	var apps apitypes.AppPage
	if status := e.do("GET", "/v1/workspaces/acme/apps?state=active", e.owner, nil, &apps); status != 200 || len(apps.Apps) != 1 {
		t.Fatalf("apps: %d %+v", status, apps)
	}
	var app apitypes.App
	if status := e.do("POST", "/v1/workspaces/acme/apps/"+apps.Apps[0].Id.String()+"/pause", e.owner, nil, &app); status != 200 || app.State != apitypes.AppStatePaused {
		t.Fatalf("pause by id: %d %+v", status, app)
	}
	fnPath := "/v1/workspaces/acme/apps/reports/functions/summarize_sales/tasks"
	raw := json.RawMessage(`{"args": [], "kwargs": {}}`)
	submit := apitypes.SubmitTasksRequest{Inputs: []apitypes.TaskInput{{Encoding: apitypes.TaskInputEncodingJson, Value: &raw}}}
	if status := e.do("POST", fnPath, e.owner, submit, &apiErr); status != http.StatusConflict {
		t.Fatalf("submit to a paused app: %d %+v", status, apiErr)
	}
	if status := e.do("POST", "/v1/workspaces/acme/apps/reports/resume", e.owner, nil, &app); status != 200 || app.State != apitypes.AppStateActive {
		t.Fatalf("resume: %d %+v", status, app)
	}
	if status := e.do("GET", "/v1/workspaces/acme/apps/Not-A-Name", e.owner, nil, &apiErr); status != http.StatusBadRequest {
		t.Fatalf("malformed app ref: %d %+v", status, apiErr)
	}

	var submitted apitypes.SubmitTasksResponse
	if status := e.do("POST", fnPath, e.owner, submit, &submitted); status != 201 {
		t.Fatalf("submit: %d %+v", status, submitted)
	}
	task := submitted.Tasks[0]
	if task.MaxAttempts != 1 || task.RootTaskId == nil || *task.RootTaskId != task.Id {
		t.Fatalf("submitted task %+v", task)
	}
	var page apitypes.TaskPage
	if status := e.do("GET", "/v1/workspaces/acme/tasks?app=reports&limit=1", e.owner, nil, &page); status != 200 ||
		len(page.Tasks) != 1 || page.Tasks[0].Pending == nil || page.Tasks[0].Pending.Message == "" {
		t.Fatalf("tasks: %d %+v", status, page)
	}
	if status := e.do("GET", "/v1/workspaces/acme/tasks?cursor=nope", e.owner, nil, &apiErr); status != http.StatusBadRequest {
		t.Fatalf("bad cursor: %d %+v", status, apiErr)
	}
	// stop is a literal segment beside the {task} routes.
	var stopped apitypes.StopTasksResponse
	if status := e.do("POST", "/v1/workspaces/acme/tasks/stop", e.owner,
		apitypes.StopTasksRequest{TaskIds: []uuid.UUID{task.Id}}, &stopped); status != 200 || len(stopped.Stopped) != 1 {
		t.Fatalf("stop: %d %+v", status, stopped)
	}
	var rerun apitypes.Task
	if status := e.do("POST", "/v1/workspaces/acme/tasks/"+task.Id.String()+"/rerun", e.owner, nil, &rerun); status != 201 || rerun.Id == task.Id {
		t.Fatalf("rerun: %d %+v", status, rerun)
	}

	var deployments apitypes.DeploymentPage
	if status := e.do("GET", "/v1/workspaces/acme/deployments?app=reports", e.owner, nil, &deployments); status != 200 || len(deployments.Deployments) != 1 {
		t.Fatalf("deployments: %d %+v", status, deployments)
	}
	deployment := "/v1/workspaces/acme/deployments/" + deployments.Deployments[0].Id.String()
	var versions apitypes.VersionPage
	if status := e.do("GET", deployment+"/versions", e.owner, nil, &versions); status != 200 || len(versions.Versions) != 1 {
		t.Fatalf("versions: %d %+v", status, versions)
	}
	var started apitypes.DeployedWorkload
	if status := e.do("POST", deployment+"/start", e.owner, apitypes.StartDeploymentRequest{Version: ptr(3)}, &apiErr); status != http.StatusNotFound {
		t.Fatalf("start on a missing version: %d %+v", status, apiErr)
	}
	if status := e.do("POST", deployment+"/start", e.owner, nil, &started); status != 200 || started.State != apitypes.WorkloadStateActive {
		t.Fatalf("start without a body: %d %+v", status, started)
	}
	req, err := http.NewRequestWithContext(t.Context(), "GET", e.url+deployment+"/logs?tail=5", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+e.owner)
	resp, err := http.DefaultClient.Do(req)
	if err != nil || resp.StatusCode != 200 || resp.Header.Get("Content-Type") != "application/x-ndjson" {
		t.Fatalf("deployment logs: %v %v", resp, err)
	}
	_ = resp.Body.Close()
	if status := e.do("GET", "/v1/workspaces/acme/containers/"+task.Id.String()+"/logs", e.owner, nil, &apiErr); status != http.StatusNotFound {
		t.Fatalf("logs of an unknown container: %d %+v", status, apiErr)
	}
	var deleted apitypes.App
	if status := e.do("DELETE", "/v1/workspaces/acme/apps/reports", e.owner, nil, &deleted); status != 200 || deleted.State != apitypes.AppStateDeleted {
		t.Fatalf("delete app: %d %+v", status, deleted)
	}
	if status := e.do("GET", "/v1/workspaces/acme/apps", e.owner, nil, &apps); status != 200 || len(apps.Apps) != 0 {
		t.Fatalf("apps after delete: %d %+v", status, apps)
	}
}

func ptr[T any](v T) *T { return &v }
