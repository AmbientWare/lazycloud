package main

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"log/slog"
	"net/http"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/notifications"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// Deleting a workspace cancels its work, waits for its containers to stop,
// deletes its objects and removes its rows, leaving other workspaces alone.
func TestWorkspaceDeletion(t *testing.T) {
	ctx := t.Context()
	pool := dbtest.New(t)
	logger := slog.New(slog.DiscardHandler)
	ident := identity.NewIdentity(pool, identity.Config{})
	exec := execution.NewExecution(pool)
	store := storage.NewStorage(pool, storagetest.Config())
	loops := &accountLoops{
		identity: ident, notifications: notifications.NewNotifications(pool, nil, logger),
		execution: exec, storage: store, logger: logger,
	}

	if _, err := ident.CreateUser(ctx, "admin@example.com", true); err != nil {
		t.Fatal(err)
	}
	keep, err := ident.CreateWorkspace(ctx, "keep", "admin@example.com")
	if err != nil {
		t.Fatal(err)
	}
	doomed, err := ident.CreateWorkspace(ctx, "doomed", "admin@example.com")
	if err != nil {
		t.Fatal(err)
	}
	token, err := ident.CreateToken(ctx, "admin@example.com", "", "admin")
	if err != nil {
		t.Fatal(err)
	}
	admin, err := ident.Authenticate(ctx, token)
	if err != nil {
		t.Fatal(err)
	}
	keepSource := uploadSource(t, store, keep.ID)
	uploadSource(t, store, doomed.ID)
	keepRelease := deployFunction(t, pool, keep.ID)
	release := deployFunction(t, pool, doomed.ID)

	// The doomed workspace has a running task on a ready container and a
	// queued one; its release keeps one warm container.
	tasks, err := exec.Submit(ctx, execution.SubmitRequest{
		Workspace: doomed.ID, App: "reports", Function: "summarize",
		Inputs: []execution.Payload{{Encoding: execution.EncodingJSON, Data: []byte(`{"args":[],"kwargs":{}}`)}, {Encoding: execution.EncodingJSON, Data: []byte(`{"args":[],"kwargs":{}}`)}},
	})
	if err != nil {
		t.Fatal(err)
	}
	host, container := readyContainer(t, pool, doomed.ID, release)
	listener := database.NewListener(pool, logger, database.ChannelClaim)
	claimed, err := exec.ClaimTasks(ctx, listener, host, container, 1, 0)
	if err != nil || len(claimed) != 1 {
		t.Fatalf("claim %v %v", claimed, err)
	}

	if _, err := ident.DeleteWorkspace(ctx, admin, "doomed"); err != nil {
		t.Fatal(err)
	}
	// Planning winds the release down: the queued task is cancelled and
	// the container drains instead of being kept warm.
	if _, err := exec.Plan(ctx, logger); err != nil {
		t.Fatal(err)
	}
	loops.deleteWorkspaces(ctx)
	for _, task := range tasks {
		got, err := exec.GetTask(ctx, listener, doomed.ID, task.ID, 0)
		if err != nil || got.Status != execution.TaskCancelled {
			t.Fatalf("task %s: %+v %v", task.ID, got.Status, err)
		}
	}
	// The container is still live, so the workspace stays until its host
	// stops it.
	if _, err := ident.GetWorkspace(ctx, admin, "doomed"); err != nil {
		t.Fatalf("finished before the container stopped: %v", err)
	}
	var state string
	if err := pool.QueryRow(ctx, "select state from containers where id = $1", uuid.UUID(container)).Scan(&state); err != nil || state != "draining" {
		t.Fatalf("container %s %v", state, err)
	}
	if _, err := exec.ApplyReport(ctx, host, execution.ContainerReport{
		Container: container, Phase: execution.ReportExited, ObservedAt: time.Now(),
		Exit: &execution.ContainerExit{Reason: execution.StopRequested},
	}); err != nil {
		t.Fatal(err)
	}

	start := time.Now()
	loops.deleteWorkspaces(ctx)
	t.Logf("deletion after the last container stopped: %s", time.Since(start))
	if _, err := ident.GetWorkspace(ctx, admin, "doomed"); err == nil {
		t.Fatal("workspace remains")
	}
	if left, err := store.DeleteWorkspaceObjects(ctx, doomed.ID); err != nil || left != 0 {
		t.Fatalf("objects left %d %v", left, err)
	}
	var rows int
	if err := pool.QueryRow(ctx, `select (select count(*) from tasks where workspace_id = $1) + (select count(*) from containers where workspace_id = $1)
		+ (select count(*) from source_objects where workspace_id = $1)`, uuid.UUID(doomed.ID)).Scan(&rows); err != nil || rows != 0 {
		t.Fatalf("rows left %d %v", rows, err)
	}
	// The other workspace keeps its warm container, release and source.
	if _, err := exec.Plan(ctx, logger); err != nil {
		t.Fatal(err)
	}
	var pending int
	if err := pool.QueryRow(ctx, "select count(*) from containers where release_id = $1 and state = 'pending'", keepRelease).Scan(&pending); err != nil || pending != 1 {
		t.Fatalf("kept workspace containers %d %v", pending, err)
	}
	if upload, err := store.RegisterSource(ctx, keep.ID, keepSource.digest, keepSource.size); err != nil || !upload.Present {
		t.Fatalf("kept source %+v %v", upload, err)
	}
}

type source struct {
	digest storage.Digest
	size   int64
}

func uploadSource(t *testing.T, store *storage.Storage, ws identity.WorkspaceID) source {
	t.Helper()
	archive := make([]byte, 256)
	_, _ = rand.Read(archive)
	digest := storage.Digest(sha256.Sum256(archive))
	upload, err := store.RegisterSource(t.Context(), ws, digest, int64(len(archive)))
	if err != nil || upload.Upload == nil {
		t.Fatalf("register %+v %v", upload, err)
	}
	req, err := http.NewRequestWithContext(t.Context(), upload.Upload.Method, upload.Upload.URL, bytes.NewReader(archive))
	if err != nil {
		t.Fatal(err)
	}
	for k, v := range upload.Upload.Headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("put source: %d", resp.StatusCode)
	}
	if upload, err := store.RegisterSource(t.Context(), ws, digest, int64(len(archive))); err != nil || !upload.Present {
		t.Fatalf("record source %+v %v", upload, err)
	}
	return source{digest: digest, size: int64(len(archive))}
}

// deployFunction inserts an active function reports/summarize with one
// warm container and returns its release.
func deployFunction(t *testing.T, pool *pgxpool.Pool, ws identity.WorkspaceID) uuid.UUID {
	t.Helper()
	spec := `{"name":"summarize","handler":"m:f","source":{"sha256":"00"},"image":{"python_version":"3.12"},
		"resources":{"cpu_millis":1000,"memory_mib":256},"autoscaler":{"min_containers":1,"max_containers":2},
		"timeout_seconds":3600,"concurrency":1,"keep_warm_seconds":10,"max_pending_tasks":100,"retry_policy":{"max_attempts":1}}`
	var release uuid.UUID
	err := pool.QueryRow(t.Context(), `
with app as (insert into apps (workspace_id, name, state) values ($1, 'reports', 'active') returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'summarize', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, $2::jsonb, sha256('spec'), sha256('src') from wl returning id)
select id from rel`, uuid.UUID(ws), spec).Scan(&release)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := pool.Exec(t.Context(), `update workloads w set active_release_id = r.id, next_version = 2
		from releases r where r.workload_id = w.id and r.id = $1`, release); err != nil {
		t.Fatal(err)
	}
	return release
}

func readyContainer(t *testing.T, pool *pgxpool.Pool, ws identity.WorkspaceID, release uuid.UUID) (compute.HostID, execution.ContainerID) {
	t.Helper()
	var host, container uuid.UUID
	err := pool.QueryRow(t.Context(), `
with host as (insert into hosts (name, token_hash, state, cpu_millis, memory_bytes, last_seen_at)
              values ('h', sha256(gen_random_uuid()::text::bytea), 'online', 4000, 1 << 32, now()) returning id),
     ctr as (insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
             select $1, $2, 'ready', host.id, 1, 1000, 1 << 28, now(), now() from host returning id)
select host.id, ctr.id from host, ctr`, uuid.UUID(ws), release).Scan(&host, &container)
	if err != nil {
		t.Fatal(err)
	}
	return compute.HostID(host), execution.ContainerID(container)
}
