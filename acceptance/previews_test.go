package acceptance

import (
	"archive/tar"
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

const previewApp = `
import lazycloud

app = lazycloud.App("demo")


@app.endpoint(name="greet")
def greet(name: str = "you") -> str:
    print("greeting", name, flush=True)
    return "hello " + name + " v1"


@app.function(name="add")
def add(a: int, b: int) -> int:
    return a + b
`

func syncArchive(t *testing.T, files map[string]string, removed ...string) []byte {
	t.Helper()
	var buf bytes.Buffer
	w := tar.NewWriter(&buf)
	for name, body := range files {
		if err := w.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Size: int64(len(body)), Typeflag: tar.TypeReg}); err != nil {
			t.Fatal(err)
		}
		if _, err := io.WriteString(w, body); err != nil {
			t.Fatal(err)
		}
	}
	for _, name := range removed {
		if err := w.WriteHeader(&tar.Header{Name: name, Mode: 0o644, Typeflag: tar.TypeReg, Format: tar.FormatPAX,
			PAXRecords: map[string]string{"LAZYCLOUD.removed": "1"}}); err != nil {
			t.Fatal(err)
		}
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestServePreviewSyncsSourceAndStops(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": previewApp})
	route := "/"
	endpoint := spec("greet", "app:greet", source, &apitypes.HttpSpec{Kind: apitypes.HttpKindEndpoint, Route: &route})

	var preview apitypes.Preview
	started := time.Now()
	if status := p.apiCall(http.MethodPost, "/v1/workspaces/ws/apps/demo/previews", apitypes.PreviewRequest{Spec: endpoint}, &preview); status != http.StatusCreated {
		t.Fatalf("create preview: %d", status)
	}
	if status := p.apiCall(http.MethodGet, "/v1/workspaces/ws/previews/"+preview.Id.String()+"?wait_seconds=60", nil, &preview); status != http.StatusOK || preview.State != apitypes.PreviewStateReady {
		t.Fatalf("preview after waiting: %d %+v", status, preview)
	}
	t.Logf("preview created to ready: %s", time.Since(started))
	if status, _, body := p.call(http.MethodPost, preview.Url, `{"name": "ada"}`); status != http.StatusOK || body != "hello ada v1" {
		t.Fatalf("preview request: %d %s", status, body)
	}

	edited := strings.Replace(previewApp, `" v1"`, `" v2"`, 1)
	req, err := http.NewRequestWithContext(t.Context(), http.MethodPost, p.api+"/v1/workspaces/ws/previews/"+preview.Id.String()+"/files",
		bytes.NewReader(syncArchive(t, map[string]string{"app.py": edited, "notes/extra.txt": "x"}, "missing.txt")))
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+p.token)
	req.Header.Set("Content-Type", "application/octet-stream")
	synced := time.Now()
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	body, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK || string(body) != "{\"removed\":1,\"written\":2}\n" {
		t.Fatalf("sync: %d %s", resp.StatusCode, body)
	}
	deadline := time.Now().Add(30 * time.Second)
	for {
		status, _, body := p.call(http.MethodPost, preview.Url, `{"name": "ada"}`)
		if status == http.StatusOK && body == "hello ada v2" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("after the sync: %d %s", status, body)
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Logf("sync to the edited handler answering: %s", time.Since(synced))

	// The container's output is the preview's output.
	outReq, err := http.NewRequestWithContext(t.Context(), http.MethodGet, p.api+"/v1/workspaces/ws/previews/"+preview.Id.String()+"/output", nil)
	if err != nil {
		t.Fatal(err)
	}
	outReq.Header.Set("Authorization", "Bearer "+p.token)
	deadline = time.Now().Add(10 * time.Second)
	for {
		resp, err := http.DefaultClient.Do(outReq)
		if err != nil {
			t.Fatal(err)
		}
		output, _ := io.ReadAll(resp.Body)
		_ = resp.Body.Close()
		if strings.Contains(string(output), "greeting ada") {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("preview output %s", output)
		}
		time.Sleep(100 * time.Millisecond)
	}

	if status := p.apiCall(http.MethodDelete, "/v1/workspaces/ws/previews/"+preview.Id.String(), nil, &preview); status != http.StatusOK || preview.State != apitypes.PreviewStateStopped {
		t.Fatalf("stop preview: %d %+v", status, preview)
	}
	if status, _, _ := p.call(http.MethodPost, preview.Url, `{}`); status != http.StatusNotFound {
		t.Fatalf("request to a stopped preview: %d", status)
	}
	deadline = time.Now().Add(30 * time.Second)
	for liveContainers(t, p.pool, preview.Id) > 0 {
		if time.Now().After(deadline) {
			t.Fatal("the stopped preview kept its container")
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func TestFunctionPreviewTakesTasksAndLapsesWithoutAFollower(t *testing.T) {
	p := startPlatform(t)
	source := p.upload(map[string]string{"app.py": previewApp})
	var preview apitypes.Preview
	if status := p.apiCall(http.MethodPost, "/v1/workspaces/ws/apps/demo/previews", apitypes.PreviewRequest{Spec: spec("add", "app:add", source, nil)}, &preview); status != http.StatusCreated {
		t.Fatalf("create preview: %d", status)
	}
	input := apitypes.TaskInput{Encoding: apitypes.TaskInputEncodingJson}
	value := []byte(`{"args": [2, 3], "kwargs": {}}`)
	input.Value = (*json.RawMessage)(&value)
	// A preview is a release of its function, so the function's submit
	// targets it by id.
	var submitted apitypes.SubmitTasksResponse
	request := apitypes.SubmitTasksRequest{Inputs: []apitypes.TaskInput{input}, ReleaseId: &preview.Id}
	if status := p.apiCall(http.MethodPost, "/v1/workspaces/ws/apps/demo/workloads/function/add/tasks", request, &submitted); status != http.StatusCreated {
		t.Fatalf("submit to the preview: %d", status)
	}
	var task apitypes.Task
	if status := p.apiCall(http.MethodGet, "/v1/workspaces/ws/tasks/"+submitted.Tasks[0].Id.String()+"?wait_seconds=60", nil, &task); status != http.StatusOK || task.Status != apitypes.TaskStatusSucceeded || task.ReleaseId != preview.Id {
		t.Fatalf("preview task: %d %+v", status, task)
	}
	// The release URL invokes the preview over HTTP too.
	status, _, body := p.call(http.MethodPost, preview.Url, `{"a": 20, "b": 22}`)
	if status != http.StatusOK || !strings.Contains(body, `"result":42`) {
		t.Fatalf("invoke the preview over HTTP: %d %s", status, body)
	}

	// Without a follower renewing it the lease lapses and the container goes.
	if _, err := p.pool.Exec(t.Context(), `update previews set lease_expires_at = now() - interval '1 second' where release_id = $1`, preview.Id); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(30 * time.Second)
	for liveContainers(t, p.pool, preview.Id) > 0 {
		if time.Now().After(deadline) {
			t.Fatal("the lapsed preview kept its container")
		}
		time.Sleep(100 * time.Millisecond)
	}
}
