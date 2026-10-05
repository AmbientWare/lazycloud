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
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/edge"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/identity/identitytest"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/notifications"
	"github.com/AmbientWare/lazycloud/internal/observability"
	"github.com/AmbientWare/lazycloud/internal/schedules"
	"github.com/AmbientWare/lazycloud/internal/secrets"
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
	compute   *compute.Compute
	identity  *identity.Identity
	github    *identitytest.GitHub
	owner     string
	outsider  string
	// distDir holds agent release archives for /install/agent.
	distDir string
}

func newEnv(t *testing.T) *env {
	t.Helper()
	return newEnvWith(t, observability.DefaultChangesConfig())
}

// newEnvWith is newEnv with the change hub's limits.
func newEnvWith(t *testing.T, changesConfig observability.ChangesConfig) *env {
	t.Helper()
	ctx := t.Context()
	pool := dbtest.New(t)
	listener := database.NewListener(pool, slog.New(slog.DiscardHandler), database.ChannelTask, database.ChannelClaim, database.ChannelLogs)
	runCtx, stop := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Go(func() { _ = listener.Run(runCtx) })
	changes := observability.NewChanges(pool, changesConfig, nil, slog.New(slog.DiscardHandler))
	probe, _, err := changes.Subscribe(identity.WorkspaceID{}, "test", nil)
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
	comp := compute.NewCompute(pool, e, compute.Config{InstallURL: "https://install.test", ServerAddress: "hosts.test:443"})
	dist := t.TempDir()
	logger := slog.New(slog.DiscardHandler)
	masterKey, err := secrets.NewFileKey(make([]byte, 32))
	if err != nil {
		t.Fatal(err)
	}
	bill := billing.NewBilling(pool, billing.Config{PublicURL: dashboardURL}, logger)
	edges, err := edge.NewEdge(pool, id, e, listener, edge.Config{URL: "https://lazycloud.test"}, logger)
	if err != nil {
		t.Fatal(err)
	}
	handler, err := api.NewHandler(api.Owners{
		Identity: id, Control: control.NewControl(pool), Storage: storage.NewStorage(pool, storagetest.Config()),
		Execution: e, Notifications: notifications.NewNotifications(pool, nil, logger), Listener: listener, Billing: bill,
		Images:        images.NewImages(pool, e, secrets.NewSecrets(pool, masterKey), images.Config{Registry: "registry.example.com", Repository: "lazycloud"}),
		Observability: observability.NewObservability(pool, observability.Config{}, logger), Changes: changes,
		Compute: comp, Schedules: schedules.NewSchedules(pool, e), Edge: edges,
	}, api.Config{PublicURL: dashboardURL, ResendWebhookSecret: webhookSecret, ClientReleaseVersion: "9.9.9", AgentDistDir: dist}, logger)
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)

	for _, email := range []string{"owner@example.com", "outsider@example.com"} {
		user, err := id.CreateUser(ctx, email, false)
		if err != nil {
			t.Fatal(err)
		}
		// Waived accounts are held to Business limits, so these tests
		// are not held to Free's; the billing tests cover plans.
		if _, err := bill.SetComplimentary(ctx, uuid.UUID(user), true); err != nil {
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
	return &env{
		t: t, pool: pool, url: server.URL, execution: e, compute: comp, identity: id, github: gh, owner: owner, outsider: outsider,
		distDir: dist,
	}
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
func (e *env) deploy() apitypes.Deployment {
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
	spec := apitypes.WorkloadSpec{
		Kind: apitypes.WorkloadKindFunction, Name: "summarize_sales", Handler: new("reports:summarize_sales"),
		Source: apitypes.SourceRef{Sha256: digest}, Image: apitypes.ImageSpec{PythonVersion: apitypes.N312},
		Resources: apitypes.Resources{CpuMillis: 1000, MemoryMib: 512},
	}
	var d apitypes.Deployment
	if status := e.do("POST", "/v1/workspaces/acme/apps/reports/deployments", e.owner,
		apitypes.DeploymentRequest{Workloads: []apitypes.WorkloadSpec{spec}}, &d); status != 200 || len(d.Releases) != 1 {
		e.t.Fatalf("deploy: %d %+v", status, d)
	}
	return d
}

// A deployed function answers on its own host, so deploy and workload
// reads say where, as they do for endpoints.
func TestDeployedFunctionsReportWhereTheyAnswer(t *testing.T) {
	e := newEnv(t)
	release := e.deploy().Releases[0]
	if release.Url == nil || !strings.HasPrefix(*release.Url, "https://summarize-sales-") || release.InvokePath == nil ||
		*release.InvokePath != "/v1/workspaces/acme/apps/reports/workloads/function/summarize_sales/invoke" {
		t.Fatalf("deploy names the function's URL: %v %v", release.Url, release.InvokePath)
	}
	var detail apitypes.WorkloadDetail
	if status := e.do("GET", "/v1/workspaces/acme/apps/reports/workloads/function/summarize_sales", e.owner, nil, &detail); status != 200 ||
		detail.Release.Url == nil || *detail.Release.Url != *release.Url {
		t.Fatalf("the workload names the same URL: %d %+v", status, detail.Release)
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
	path := "/v1/workspaces/acme/apps/reports/workloads/function/summarize_sales"
	if status := e.do("GET", path, e.outsider, nil, &apiErr); status != 403 || apiErr.Code != apitypes.Forbidden {
		t.Fatalf("outsider: %d %+v", status, apiErr)
	}
	if status := e.do("GET", path, e.owner, nil, &apiErr); status != 404 || apiErr.Code != apitypes.NotFound {
		t.Fatalf("missing workload: %d %+v", status, apiErr)
	}
	// The schema rejects a malformed digest before any handler runs.
	if status := e.do("POST", "/v1/workspaces/acme/sources", e.owner,
		map[string]any{"sha256": "XYZ", "size_bytes": 10}, &apiErr); status != 400 || apiErr.Code != apitypes.InvalidRequest {
		t.Fatalf("bad digest: %d %+v", status, apiErr)
	}
	// Deploying a source that was never uploaded is refused.
	spec := map[string]any{
		"kind": "function", "name": "f", "handler": "m:f", "source": map[string]any{"sha256": strings.Repeat("0", 64)},
		"image": map[string]any{"python_version": "3.12"}, "resources": map[string]any{"cpu_millis": 1000, "memory_mib": 512},
	}
	if status := e.do("POST", "/v1/workspaces/acme/apps/reports/deployments", e.owner,
		map[string]any{"workloads": []any{spec}}, &apiErr); status != 400 || !strings.Contains(apiErr.Message, "not uploaded") {
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
	fnPath := "/v1/workspaces/acme/apps/reports/workloads/function/summarize_sales"
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

func TestWaitTasksAuthorizesBoundsAndInlinesResults(t *testing.T) {
	e := newEnv(t)
	e.deploy()
	var submitted apitypes.SubmitTasksResponse
	raw := json.RawMessage(`{"args": [], "kwargs": {}}`)
	if status := e.do("POST", "/v1/workspaces/acme/apps/reports/workloads/function/summarize_sales/tasks", e.owner,
		apitypes.SubmitTasksRequest{Inputs: []apitypes.TaskInput{{Encoding: apitypes.TaskInputEncodingJson, Value: &raw}}},
		&submitted); status != 201 {
		t.Fatalf("submit: %d", status)
	}
	id := submitted.Tasks[0].Id
	wait := func(token string, body any, out any) int {
		return e.do("POST", "/v1/workspaces/acme/tasks/wait", token, body, out)
	}
	var apiErr apitypes.Error
	if status := wait(e.outsider, apitypes.WaitTasksRequest{TaskIds: []uuid.UUID{id}}, &apiErr); status != 403 {
		t.Fatalf("outsider: %d %+v", status, apiErr)
	}
	tooMany := make([]uuid.UUID, 1001)
	for n := range tooMany {
		tooMany[n] = uuid.New()
	}
	sixtyOne := 61
	for name, body := range map[string]apitypes.WaitTasksRequest{
		"no ids": {TaskIds: []uuid.UUID{}}, "1001 ids": {TaskIds: tooMany}, "repeated id": {TaskIds: []uuid.UUID{id, id}},
		"61 seconds": {TaskIds: []uuid.UUID{id}, WaitSeconds: &sixtyOne},
	} {
		if status := wait(e.owner, body, &apiErr); status != 400 || apiErr.Code != apitypes.InvalidRequest {
			t.Fatalf("%s: %d %+v", name, status, apiErr)
		}
	}
	unknown := uuid.New()
	if status := wait(e.owner, apitypes.WaitTasksRequest{TaskIds: []uuid.UUID{id, unknown}}, &apiErr); status != 404 ||
		apiErr.Code != apitypes.NotFound || !strings.Contains(apiErr.Message, unknown.String()) {
		t.Fatalf("unknown task: %d %+v", status, apiErr)
	}

	go e.runOnHost("5500")
	thirty := 30
	var got apitypes.WaitTasksResponse
	if status := wait(e.owner, apitypes.WaitTasksRequest{TaskIds: []uuid.UUID{id}, WaitSeconds: &thirty}, &got); status != 200 ||
		len(got.Tasks) != 1 || got.Tasks[0].Task.Status != apitypes.TaskStatusSucceeded || got.Tasks[0].ResultOmitted ||
		got.Tasks[0].Result == nil || string(*got.Tasks[0].Result.Value) != "5500" {
		t.Fatalf("wait: %d %+v", status, got)
	}
}

// runEvents posts a RunTask request and returns the response's status and
// content type with a reader of its events.
func (e *env) runEvents(path string, body apitypes.RunTaskRequest) (int, string, func() (apitypes.TaskRunEvent, bool)) {
	e.t.Helper()
	encoded, err := json.Marshal(body)
	if err != nil {
		e.t.Fatal(err)
	}
	req, err := http.NewRequestWithContext(e.t.Context(), "POST", e.url+path, bytes.NewReader(encoded))
	if err != nil {
		e.t.Fatal(err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+e.owner)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	e.t.Cleanup(func() { _ = resp.Body.Close() })
	scanner := bufio.NewScanner(resp.Body)
	return resp.StatusCode, resp.Header.Get("Content-Type"), func() (apitypes.TaskRunEvent, bool) {
		for scanner.Scan() {
			if len(bytes.TrimSpace(scanner.Bytes())) == 0 {
				continue
			}
			var event apitypes.TaskRunEvent
			if err := json.Unmarshal(scanner.Bytes(), &event); err != nil {
				e.t.Fatalf("event %q: %v", scanner.Text(), err)
			}
			return event, true
		}
		return apitypes.TaskRunEvent{}, false
	}
}

func TestRunTaskStreamsTheTaskItsLogsAndResult(t *testing.T) {
	e := newEnv(t)
	e.deploy()
	fnPath := "/v1/workspaces/acme/apps/reports/workloads/function/summarize_sales"
	raw := json.RawMessage(`{"args": [[1200, 3500, 800]], "kwargs": {}}`)
	input := apitypes.TaskInput{Encoding: apitypes.TaskInputEncodingJson, Value: &raw}

	status, contentType, next := e.runEvents(fnPath+"/run", apitypes.RunTaskRequest{Input: input})
	if status != 200 || contentType != "application/x-ndjson" {
		t.Fatalf("run: %d %s", status, contentType)
	}
	admitted, ok := next()
	if !ok || admitted.Task == nil || admitted.Task.Status != apitypes.TaskStatusQueued || admitted.Log != nil {
		t.Fatalf("admitted event %+v", admitted)
	}
	go e.runOnHost("5500")
	logged, ok := next()
	if !ok || logged.Log == nil || logged.Log.Data != "summing\n" || logged.Log.TaskId != admitted.Task.Id {
		t.Fatalf("log event %+v", logged)
	}
	final, ok := next()
	if !ok || final.Task == nil || final.Task.Status != apitypes.TaskStatusSucceeded ||
		final.Result == nil || final.Result.Value == nil || string(*final.Result.Value) != "5500" {
		t.Fatalf("final event %+v", final)
	}
	if extra, ok := next(); ok {
		t.Fatalf("event after the final one: %+v", extra)
	}

	// A task still queued when the wait passes ends the stream with its state.
	started := time.Now()
	_, _, next = e.runEvents(fnPath+"/run?wait_seconds=1", apitypes.RunTaskRequest{Input: input})
	if first, ok := next(); !ok || first.Task == nil {
		t.Fatalf("admitted event %+v", first)
	}
	last, ok := next()
	if !ok || last.Task == nil || last.Task.Status != apitypes.TaskStatusQueued || last.Result != nil {
		t.Fatalf("event after the wait %+v", last)
	}
	if _, ok := next(); ok || time.Since(started) > 10*time.Second {
		t.Fatalf("the stream did not end after the wait: %s", time.Since(started))
	}

	// Admission failures are error responses, not streams.
	var apiErr apitypes.Error
	missing := "/v1/workspaces/acme/apps/reports/workloads/function/missing/run"
	if status := e.do("POST", missing, e.owner, apitypes.RunTaskRequest{Input: input}, &apiErr); status != 404 || apiErr.Code != apitypes.NotFound {
		t.Fatalf("missing function: %d %+v", status, apiErr)
	}
	if status := e.do("POST", fnPath+"/run", e.outsider, apitypes.RunTaskRequest{Input: input}, &apiErr); status != 403 || apiErr.Code != apitypes.Forbidden {
		t.Fatalf("outsider: %d %+v", status, apiErr)
	}
}
