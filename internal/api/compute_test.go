package api_test

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
)

// get fetches path without credentials and returns the status, body and
// headers.
func (e *env) get(path string) (int, []byte, http.Header) {
	e.t.Helper()
	req, err := http.NewRequestWithContext(e.t.Context(), http.MethodGet, e.url+path, nil)
	if err != nil {
		e.t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		e.t.Fatal(err)
	}
	return resp.StatusCode, body, resp.Header
}

// archive writes a release archive for version and amd64 and returns its
// digest.
func (e *env) archive(version string, data []byte) string {
	e.t.Helper()
	dir := filepath.Join(e.distDir, version)
	if err := os.MkdirAll(dir, 0o750); err != nil {
		e.t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, compute.ArchiveName("amd64")), data, 0o600); err != nil {
		e.t.Fatal(err)
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func TestAgentInstallsServePublishedReleaseArchives(t *testing.T) {
	ctx := t.Context()
	e := newEnv(t)
	data := make([]byte, 4096)
	_, _ = rand.Read(data)
	digest := e.archive("1.0.0", data)
	release := compute.AgentRelease{Version: "1.0.0", SHA256: map[string]string{"amd64": digest}}
	if err := e.compute.PublishAgentRelease(ctx, release); err != nil {
		t.Fatal(err)
	}
	if err := e.compute.PublishAgentRelease(ctx, release); err != nil {
		t.Fatalf("republish the same release: %v", err)
	}
	var conflict *compute.ConflictError
	if err := e.compute.PublishAgentRelease(ctx, compute.AgentRelease{Version: "1.0.0", SHA256: map[string]string{"amd64": strings.Repeat("0", 64)}}); !errors.As(err, &conflict) {
		t.Fatalf("republish 1.0.0 with another digest: %v, want ConflictError", err)
	}

	status, script, _ := e.get("/install/agent")
	if status != 200 || !strings.Contains(string(script), `CONFIGURED_VERSION="1.0.0"`) ||
		!strings.Contains(string(script), `CONFIGURED_AMD64_SHA256="`+digest+`"`) {
		t.Fatalf("install script %d does not carry the target release:\n%.400s", status, script)
	}
	for _, path := range []string{"/install/agent/1.0.0/linux/amd64", "/install/agent/linux/amd64"} {
		status, body, header := e.get(path)
		if status != 200 || string(body) != string(data) || header.Get("X-Lazycloud-Agent-Version") != "1.0.0" {
			t.Fatalf("GET %s: %d, %d bytes, version %q", path, status, len(body), header.Get("X-Lazycloud-Agent-Version"))
		}
	}
	for path, want := range map[string]int{
		"/install/agent/2.0.0/linux/amd64":    404,
		"/install/agent/1.0.0/linux/arm64":    404,
		"/install/agent/1.0.0/darwin/amd64":   404,
		"/install/agent/..%2Fetc/linux/amd64": 404,
	} {
		if status, _, _ := e.get(path); status != want {
			t.Errorf("GET %s: %d, want %d", path, status, want)
		}
	}
}

func TestAccountComputeRefusesWorkspaceTokensAndForeignWorkspaces(t *testing.T) {
	ctx := t.Context()
	e := newEnv(t)
	restricted, err := e.identity.CreateToken(ctx, "owner@example.com", "acme", "ci")
	if err != nil {
		t.Fatal(err)
	}
	join := map[string]any{"name": "gpu-1", "workspaces": []string{"acme"}}
	for _, op := range []struct {
		method, path string
		body         any
	}{
		{"GET", "/v1/aws-connection", nil},
		{"POST", "/v1/aws-connection", map[string]any{"account_id": "111111111111"}},
		{"GET", "/v1/machines", nil},
		{"POST", "/v1/machines/join-command", join},
		{"DELETE", "/v1/machines/gpu-1", nil},
		{"GET", "/v1/compute/instances", nil},
	} {
		var apiErr apitypes.Error
		if status := e.do(op.method, op.path, restricted, op.body, &apiErr); status != 403 || apiErr.Code != apitypes.Forbidden {
			t.Errorf("%s %s with a workspace token: %d %+v, want 403", op.method, op.path, status, apiErr)
		}
	}
	if status := e.do("GET", "/v1/machines", e.owner, nil, nil); status != 200 {
		t.Fatalf("list machines with an account token: %d", status)
	}

	var apiErr apitypes.Error
	if status := e.do("POST", "/v1/machines/join-command", e.owner, join, &apiErr); status != 503 {
		t.Fatalf("join before an agent release is published: %d %+v, want 503", status, apiErr)
	}
	if err := e.compute.PublishAgentRelease(ctx, compute.AgentRelease{Version: "1.0.0", SHA256: map[string]string{"amd64": strings.Repeat("a", 64)}}); err != nil {
		t.Fatal(err)
	}
	if status := e.do("POST", "/v1/machines/join-command", e.outsider, join, &apiErr); status != 400 || apiErr.Code != apitypes.InvalidRequest {
		t.Fatalf("join serving a workspace the caller does not own: %d %+v, want 400", status, apiErr)
	}
	var cmd apitypes.MachineJoinCommand
	if status := e.do("POST", "/v1/machines/join-command", e.owner, join, &cmd); status != 201 ||
		!strings.Contains(cmd.Command, "--join-token") || cmd.Machine.Lifecycle != apitypes.MachineLifecycle(compute.PhaseRequested) {
		t.Fatalf("owner's join command: %d %+v", status, cmd)
	}
}

func TestFleetIsForAdministratorsOnly(t *testing.T) {
	ctx := t.Context()
	e := newEnv(t)
	if _, err := e.identity.CreateUser(ctx, "admin@example.com", true); err != nil {
		t.Fatal(err)
	}
	admin, err := e.identity.CreateToken(ctx, "admin@example.com", "", "test")
	if err != nil {
		t.Fatal(err)
	}
	var platform uuid.UUID
	if err := e.pool.QueryRow(ctx, `
insert into hosts (name, token_hash, state, last_seen_at, cpu_millis, memory_bytes)
values ('platform-1', sha256(random()::text::bytea), 'online', now(), 4000, 8 << 30) returning id`).Scan(&platform); err != nil {
		t.Fatal(err)
	}
	if _, err := e.pool.Exec(ctx, `
insert into hosts (name, token_hash, state, last_seen_at, cpu_millis, memory_bytes, kind, account_id)
values ('gpu-1', sha256(random()::text::bytea), 'online', now(), 4000, 8 << 30, 'machine', (select id from users where email = 'owner@example.com'))`); err != nil {
		t.Fatal(err)
	}

	for _, path := range []string{"/v1/fleet", "/v1/fleet/nodes"} {
		var apiErr apitypes.Error
		if status := e.do("GET", path, e.owner, nil, &apiErr); status != 403 || apiErr.Code != apitypes.Forbidden {
			t.Errorf("GET %s as a user: %d %+v, want 403", path, status, apiErr)
		}
	}
	var nodes struct {
		Nodes []apitypes.FleetNode `json:"nodes"`
	}
	if status := e.do("GET", "/v1/fleet/nodes", admin, nil, &nodes); status != 200 || len(nodes.Nodes) != 1 ||
		nodes.Nodes[0].Id != platform || nodes.Nodes[0].State != apitypes.FleetState(compute.FleetServing) {
		t.Fatalf("fleet nodes as an administrator: %d %+v, want only the serving platform host", status, nodes)
	}
	if status := e.do("GET", "/v1/fleet", admin, nil, nil); status != 200 {
		t.Fatalf("fleet summary as an administrator: %d", status)
	}
}
