package storage_test

import (
	"bytes"
	"crypto/rand"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
	. "github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/storage/storagetest"
)

// fixture is a workspace with one app, function, release and host.
type fixture struct {
	t       testing.TB
	pool    *pgxpool.Pool
	storage *Storage
	ws      identity.WorkspaceID
	release uuid.UUID
	host    compute.HostID
}

func newFixture(t testing.TB, spec string) *fixture {
	t.Helper()
	pool := dbtest.New(t)
	f := &fixture{t: t, pool: pool}
	f.storage = NewStorage(pool, withLinks(t, func() *Storage { return f.storage }))
	f.ws = f.workspace("ws-" + uuid.NewString()[:8])
	var app, workload uuid.UUID
	f.exec(`insert into apps (workspace_id, name, state) values ($1, 'app', 'active') returning id`, &app, uuid.UUID(f.ws))
	f.exec(`insert into workloads (app_id, kind, name, desired_state) values ($1, 'function', 'fn', 'active') returning id`, &workload, app)
	digest := make([]byte, 32)
	_, _ = rand.Read(digest)
	f.exec(`insert into releases (workload_id, version, spec, spec_digest, source_sha256) values ($1, 1, $2, $3, $3) returning id`,
		&f.release, workload, spec, digest)
	if _, err := pool.Exec(t.Context(), `update workloads set active_release_id = $1 where id = $2`, f.release, workload); err != nil {
		t.Fatal(err)
	}
	var host uuid.UUID
	f.exec(`insert into hosts (name, token_hash, state, cpu_millis, memory_bytes) values ('h', $1, 'online', 1000, 1000) returning id`, &host, digest)
	f.host = compute.HostID(host)
	return f
}

func (f *fixture) exec(sql string, into *uuid.UUID, args ...any) {
	f.t.Helper()
	if err := f.pool.QueryRow(f.t.Context(), sql, args...).Scan(into); err != nil {
		f.t.Fatalf("%s: %v", sql, err)
	}
}

func (f *fixture) workspace(name string) identity.WorkspaceID {
	f.t.Helper()
	var id uuid.UUID
	f.exec(`insert into workspaces (name) values ($1) returning id`, &id, name)
	dbtest.OwnWorkspaces(f.t, f.pool)
	return identity.WorkspaceID(id)
}

// container is a live container of the release on the fixture's host.
func (f *fixture) container() uuid.UUID {
	f.t.Helper()
	var id uuid.UUID
	f.exec(`insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes)
		values ($1, $2, 'ready', $3, 1, 1000, 1000) returning id`, &id, uuid.UUID(f.ws), f.release, uuid.UUID(f.host))
	return id
}

func (f *fixture) stop(container uuid.UUID, reason string) {
	f.t.Helper()
	if _, err := f.pool.Exec(f.t.Context(), `update containers set state = 'stopped', stop_reason = $2, stopped_at = now() where id = $1`, container, reason); err != nil {
		f.t.Fatal(err)
	}
}

func (f *fixture) task() uuid.UUID {
	f.t.Helper()
	var id uuid.UUID
	f.exec(`insert into tasks (workspace_id, workload_id, release_id, status, max_attempts)
		select $1, workload_id, id, 'running', 1 from releases where id = $2 returning id`, &id, uuid.UUID(f.ws), f.release)
	return id
}

// send makes a presigned PUT with body and returns the status and ETag.
func send(t *testing.T, url string, body []byte) (int, string) {
	t.Helper()
	req, err := http.NewRequestWithContext(t.Context(), http.MethodPut, url, bytes.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.ContentLength = int64(len(body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("put: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()
	_, _ = io.Copy(io.Discard, resp.Body)
	return resp.StatusCode, resp.Header.Get("ETag")
}

// withLinks is the development store with download links served the way
// the API serves them, by the storage that store returns.
func withLinks(t testing.TB, store func() *Storage) Config {
	t.Helper()
	links := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		url, err := store().OpenLink(r.Context(), r.URL.Path[len("/v1/links/"):], r.Method == http.MethodHead)
		if err != nil {
			http.Error(w, err.Error(), http.StatusNotFound)
			return
		}
		http.Redirect(w, r, url, http.StatusFound)
	}))
	t.Cleanup(links.Close)
	cfg := storagetest.Config()
	cfg.Links = Links{URL: links.URL + "/v1/links/", Key: []byte("download links of the storage tests")}
	return cfg
}

func get(t *testing.T, url string) (int, []byte, http.Header) {
	t.Helper()
	req, err := http.NewRequestWithContext(t.Context(), http.MethodGet, url, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	return resp.StatusCode, data, resp.Header
}
