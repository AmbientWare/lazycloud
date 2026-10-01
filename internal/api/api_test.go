package api_test

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/api"
	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/identity/identitytest"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/notifications"
	"github.com/AmbientWare/lazycloud/internal/observability"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// dashboardURL is the public origin the handler trusts for browser
// requests; webhookSecret signs test Resend deliveries.
const (
	dashboardURL  = "https://dashboard.test"
	webhookSecret = "whsec_dGVzdC13ZWJob29rLXNlY3JldC1rZXk="
)

type env struct {
	t         *testing.T
	pool      *pgxpool.Pool
	url       string
	execution *execution.Execution
	identity  *identity.Identity
	github    *identitytest.GitHub
	owner     string
	outsider  string
}

func newEnv(t *testing.T) *env {
	t.Helper()
	ctx := t.Context()
	pool := dbtest.New(t)
	listener := database.NewListener(pool, slog.New(slog.DiscardHandler), database.ChannelTask, database.ChannelClaim, database.ChannelLogs)
	runCtx, stop := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Go(func() { _ = listener.Run(runCtx) })
	changes := observability.NewChanges(pool, observability.DefaultChangesConfig(), nil, slog.New(slog.DiscardHandler))
	probe, _, err := changes.Subscribe(identity.WorkspaceID{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	wg.Go(func() { _ = changes.Run(runCtx) })
	t.Cleanup(func() { stop(); wg.Wait() })
	// The hub resets every subscriber once it listens; streams opened
	// after that see every later change.
	select {
	case <-probe.Reset():
		probe.Close()
	case <-time.After(10 * time.Second):
		t.Fatal("the change hub did not start listening")
	}

	gh := identitytest.NewGitHub(t, dashboardURL+identity.GitHubCallbackPath)
	id := identity.NewIdentity(pool, identity.Config{PublicURL: dashboardURL, GitHub: identity.GitHubConfig{
		ClientID: identitytest.ClientID, ClientSecret: identitytest.ClientSecret, OAuthURL: gh.URL, APIURL: gh.URL,
	}})
	e := execution.NewExecution(pool)
	logger := slog.New(slog.DiscardHandler)
	handler, err := api.NewHandler(api.Owners{
		Identity: id, Control: control.NewControl(pool), Storage: storage.NewStorage(pool, storagetest.Config()),
		Execution: e, Notifications: notifications.NewNotifications(pool, nil, logger), Listener: listener,
		Images:        images.NewImages(pool, e, images.Config{Registry: "registry.example.com", Repository: "lazycloud"}),
		Observability: observability.NewObservability(pool, observability.Config{}, logger), Changes: changes,
	}, api.Config{PublicURL: dashboardURL, ResendWebhookSecret: webhookSecret, ClientReleaseVersion: "9.9.9"}, logger)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)

	for _, email := range []string{"owner@example.com", "outsider@example.com"} {
		if _, err := id.CreateUser(ctx, email, false); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := id.CreateWorkspace(ctx, "acme", "owner@example.com"); err != nil {
		t.Fatal(err)
	}
	owner, err := id.CreateToken(ctx, "owner@example.com", "", "test")
	if err != nil {
		t.Fatal(err)
	}
	outsider, err := id.CreateToken(ctx, "outsider@example.com", "", "test")
	if err != nil {
		t.Fatal(err)
	}
	return &env{t: t, pool: pool, url: server.URL, execution: e, identity: id, github: gh, owner: owner, outsider: outsider}
}

// do sends a request and decodes the response into out. It is safe off the
// test goroutine: failures are reported and return status 0.
func (e *env) do(method, path, token string, body any, out any) int {
	e.t.Helper()
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			e.t.Error(err)
			return 0
		}
		reader = bytes.NewReader(encoded)
	}
	req, err := http.NewRequestWithContext(e.t.Context(), method, e.url+path, reader)
	if err != nil {
		e.t.Error(err)
		return 0
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Error(err)
		return 0
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		e.t.Error(err)
		return 0
	}
	if out != nil {
		if err := json.Unmarshal(data, out); err != nil {
			e.t.Errorf("%s %s: decode %q: %v", method, path, data, err)
		}
	}
	return resp.StatusCode
}

// deploy uploads a source archive through the returned presigned URL and
// deploys a function using it.
func (e *env) deploy() {
	e.t.Helper()
	archive := make([]byte, 512)
	_, _ = rand.Read(archive)
	sum := sha256.Sum256(archive)
	digest := hex.EncodeToString(sum[:])
	var upload apitypes.SourceUpload
	if status := e.do("POST", "/v1/workspaces/acme/sources", e.owner,
		apitypes.SourceUploadRequest{Sha256: digest, SizeBytes: int64(len(archive))}, &upload); status != 200 || upload.Upload == nil {
		e.t.Fatalf("source upload: %d %+v", status, upload)
	}
	req, err := http.NewRequestWithContext(e.t.Context(), string(upload.Upload.Method), upload.Upload.Url, bytes.NewReader(archive))
	if err != nil {
		e.t.Fatal(err)
	}
	for k, v := range upload.Upload.Headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil || resp.StatusCode != 200 {
		e.t.Fatalf("put source: %v %v", resp, err)
	}
	_ = resp.Body.Close()
	if status := e.do("POST", "/v1/workspaces/acme/sources", e.owner,
		apitypes.SourceUploadRequest{Sha256: digest, SizeBytes: int64(len(archive))}, &upload); status != 200 || !upload.Present {
		e.t.Fatalf("source registration: %d %+v", status, upload)
	}
	spec := apitypes.FunctionSpec{
		Name: "summarize_sales", Handler: "reports:summarize_sales",
		Source: apitypes.SourceRef{Sha256: digest}, Image: apitypes.ImageSpec{PythonVersion: apitypes.N312},
		Resources: apitypes.Resources{CpuMillis: 1000, MemoryMib: 512},
	}
	var d apitypes.Deployment
	if status := e.do("POST", "/v1/workspaces/acme/apps/reports/deployments", e.owner,
		apitypes.DeploymentRequest{Functions: []apitypes.FunctionSpec{spec}}, &d); status != 200 || len(d.Releases) != 1 {
		e.t.Fatalf("deploy: %d %+v", status, d)
	}
}

func TestAuthenticationAuthorizationAndValidation(t *testing.T) {
	e := newEnv(t)
	var apiErr apitypes.Error
	if status := e.do("GET", "/v1/me", "", nil, &apiErr); status != 401 || apiErr.Code != apitypes.Unauthenticated {
		t.Fatalf("no token: %d %+v", status, apiErr)
	}
	if status := e.do("GET", "/v1/me", "lc_forged", nil, &apiErr); status != 401 {
		t.Fatalf("unknown token: %d", status)
	}
	var me apitypes.Me
	if status := e.do("GET", "/v1/me", e.owner, nil, &me); status != 200 || len(me.Workspaces) != 1 || me.User.Email != "owner@example.com" {
		t.Fatalf("me: %d %+v", status, me)
	}
	path := "/v1/workspaces/acme/apps/reports/functions/summarize_sales"
	if status := e.do("GET", path, e.outsider, nil, &apiErr); status != 403 || apiErr.Code != apitypes.Forbidden {
		t.Fatalf("outsider: %d %+v", status, apiErr)
	}
	if status := e.do("GET", path, e.owner, nil, &apiErr); status != 404 || apiErr.Code != apitypes.NotFound {
		t.Fatalf("missing function: %d %+v", status, apiErr)
	}
	// The schema rejects a malformed digest before any handler runs.
	if status := e.do("POST", "/v1/workspaces/acme/sources", e.owner,
		map[string]any{"sha256": "XYZ", "size_bytes": 10}, &apiErr); status != 400 || apiErr.Code != apitypes.InvalidRequest {
		t.Fatalf("bad digest: %d %+v", status, apiErr)
	}
	// Deploying a source that was never uploaded is refused.
	spec := map[string]any{
		"name": "f", "handler": "m:f", "source": map[string]any{"sha256": strings.Repeat("0", 64)},
		"image": map[string]any{"python_version": "3.12"}, "resources": map[string]any{"cpu_millis": 1000, "memory_mib": 512},
	}
	if status := e.do("POST", "/v1/workspaces/acme/apps/reports/deployments", e.owner,
		map[string]any{"functions": []any{spec}}, &apiErr); status != 400 || !strings.Contains(apiErr.Message, "not uploaded") {
		t.Fatalf("missing source: %d %+v", status, apiErr)
	}
}

// runOnHost claims the task as a host would and completes it with result
// after writing a log line. It runs off the test goroutine, so it reports
// failures without stopping the test.
func (e *env) runOnHost(result string) {
	ctx := e.t.Context()
	var hostID, containerID uuid.UUID
	err := e.pool.QueryRow(ctx, `
with host as (insert into hosts (name, token_hash, state, cpu_millis, memory_bytes)
              values ('h', sha256('h'), 'online', 4000, 1 << 32) returning id),
     ctr as (insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes)
             select w.id, r.id, 'ready', host.id, 1, 1000, 1 << 28
             from workspaces w, releases r, host returning id)
select host.id, ctr.id from host, ctr`).Scan(&hostID, &containerID)
	if err != nil {
		e.t.Error(err)
		return
	}
	host, container := compute.HostID(hostID), execution.ContainerID(containerID)
	listener := database.NewListener(e.pool, slog.New(slog.DiscardHandler), database.ChannelClaim)
	claimed, err := e.execution.ClaimTasks(ctx, listener, host, container, 1, 0)
	if err != nil || len(claimed) != 1 {
		e.t.Errorf("claim: %v %v", claimed, err)
		return
	}
	if err := e.execution.AppendLogs(ctx, host, container, []execution.LogLine{{
		Attempt: claimed[0].Attempt, Stream: execution.LogStdout, Data: "summing\n", Time: time.Now(),
	}}); err != nil {
		e.t.Error(err)
		return
	}
	time.Sleep(100 * time.Millisecond)
	if err := e.execution.CompleteAttempt(ctx, host, container, execution.AttemptOutcome{
		Attempt: claimed[0].Attempt, State: execution.AttemptSucceeded,
		Result: &execution.Payload{Encoding: execution.EncodingJSON, Data: []byte(result)},
	}); err != nil {
		e.t.Error(err)
		return
	}
}

func TestSubmitWaitFollowLogsAndResult(t *testing.T) {
	e := newEnv(t)
	e.deploy()
	fnPath := "/v1/workspaces/acme/apps/reports/functions/summarize_sales"
	// A value beyond float64 precision passes through unchanged.
	raw := json.RawMessage(`{"args": [[1200, 3500, 800], 9007199254740993], "kwargs": {}}`)
	var submitted apitypes.SubmitTasksResponse
	if status := e.do("POST", fnPath+"/tasks", e.owner, apitypes.SubmitTasksRequest{
		Inputs: []apitypes.TaskInput{{Encoding: apitypes.TaskInputEncodingJson, Value: &raw}},
	}, &submitted); status != 201 || len(submitted.Tasks) != 1 || submitted.Tasks[0].Status != apitypes.TaskStatusQueued {
		t.Fatalf("submit: %d %+v", status, submitted)
	}
	taskPath := "/v1/workspaces/acme/tasks/" + submitted.Tasks[0].Id.String()
	var stored []byte
	if err := e.pool.QueryRow(t.Context(), "select data from task_inputs").Scan(&stored); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(stored), "9007199254740993") {
		t.Fatalf("stored input %s lost precision", stored)
	}
	var apiErr apitypes.Error
	if status := e.do("GET", taskPath+"/result", e.owner, nil, &apiErr); status != 409 || apiErr.Code != apitypes.TaskNotFinished {
		t.Fatalf("early result: %d %+v", status, apiErr)
	}

	// Follow the log over HTTP while the host runs the task.
	req, err := http.NewRequestWithContext(t.Context(), "GET", e.url+taskPath+"/logs?follow=true", nil)
	if err != nil {
		t.Fatal(err)
	}
	req.Header.Set("Authorization", "Bearer "+e.owner)
	resp, err := http.DefaultClient.Do(req)
	if err != nil || resp.StatusCode != 200 {
		t.Fatalf("logs: %v %v", resp, err)
	}
	defer func() { _ = resp.Body.Close() }()

	waited := make(chan apitypes.Task, 1)
	go func() {
		var task apitypes.Task
		e.do("GET", taskPath+"?wait_seconds=30", e.owner, nil, &task)
		waited <- task
	}()
	go e.runOnHost("5500")

	scanner := bufio.NewScanner(resp.Body)
	var lines []apitypes.LogEntry
	for scanner.Scan() {
		var entry apitypes.LogEntry
		if err := json.Unmarshal(scanner.Bytes(), &entry); err != nil {
			t.Fatalf("log line %q: %v", scanner.Text(), err)
		}
		lines = append(lines, entry)
	}
	// The stream ended on its own once the task finished.
	if len(lines) != 1 || lines[0].Data != "summing\n" || lines[0].Attempt != 1 {
		t.Fatalf("log lines %+v", lines)
	}
	select {
	case task := <-waited:
		if task.Status != apitypes.TaskStatusSucceeded {
			t.Fatalf("waited task %+v", task)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("wait did not return after completion")
	}
	var result apitypes.Payload
	if status := e.do("GET", taskPath+"/result", e.owner, nil, &result); status != 200 || result.Value == nil || string(*result.Value) != "5500" {
		t.Fatalf("result: %d %+v", status, result)
	}
}
