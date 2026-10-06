package execution

import (
	"context"
	"log/slog"
	"sync"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

type functionFixture struct {
	workspace identity.WorkspaceID
	release   uuid.UUID
}

// deployedFunction inserts an active function "reports/summarize" whose
// active release has spec.
func deployedFunction(t *testing.T, pool *pgxpool.Pool, spec string) functionFixture {
	t.Helper()
	var f functionFixture
	var ws uuid.UUID
	err := pool.QueryRow(t.Context(), `
with ws as (insert into workspaces (name) values ('ws') returning id),
     app as (insert into apps (workspace_id, name, state) select id, 'reports', 'active' from ws returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'summarize', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, $1::jsonb, sha256('spec'), sha256('src') from wl returning id, workload_id)
select ws.id, rel.id from ws, rel`, spec).Scan(&ws, &f.release)
	if err != nil {
		t.Fatalf("insert function: %v", err)
	}
	dbtest.OwnWorkspaces(t, pool)
	if _, err := pool.Exec(t.Context(), "update workloads set active_release_id = $1, next_version = 2", f.release); err != nil {
		t.Fatal(err)
	}
	f.workspace = identity.WorkspaceID(ws)
	return f
}

// placedContainer inserts an online host and a container of release in
// state on it.
func placedContainer(t *testing.T, pool *pgxpool.Pool, f functionFixture, state ContainerState, slots int) (compute.HostID, ContainerID) {
	t.Helper()
	var host, container uuid.UUID
	err := pool.QueryRow(t.Context(), `
with host as (insert into hosts (name, token_hash, state, cpu_millis, memory_bytes)
              values ('h', sha256(gen_random_uuid()::text::bytea), 'online', 4000, 1 << 32) returning id),
     ctr as (insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at)
             select $1, $2, $3, host.id, $4, 1000, 1 << 28, now() from host returning id)
select host.id, ctr.id from host, ctr`, uuid.UUID(f.workspace), f.release, string(state), slots).Scan(&host, &container)
	if err != nil {
		t.Fatalf("insert container: %v", err)
	}
	return compute.HostID(host), ContainerID(container)
}

// listen runs a listener on the execution channels for the test.
func listen(t *testing.T, pool *pgxpool.Pool) *database.Listener {
	t.Helper()
	l := database.NewListener(pool, slog.New(slog.DiscardHandler),
		database.ChannelHost, database.ChannelTask, database.ChannelTaskFinished, database.ChannelClaim)
	ctx, cancel := context.WithCancel(context.Background())
	var wg sync.WaitGroup
	wg.Go(func() { _ = l.Run(ctx) })
	t.Cleanup(func() {
		cancel()
		wg.Wait()
	})
	return l
}

func jsonInputs(n int) []TaskInput {
	inputs := make([]TaskInput, n)
	for i := range inputs {
		inputs[i] = TaskInput{Payload: Payload{Encoding: EncodingJSON, Data: []byte(`{"args": [1], "kwargs": {}}`)}}
	}
	return inputs
}

func submit(t *testing.T, e *Execution, f functionFixture, n int) []Task {
	t.Helper()
	tasks, err := e.Submit(t.Context(), SubmitRequest{Workspace: f.workspace, App: "reports", Function: "summarize", Inputs: jsonInputs(n)})
	if err != nil {
		t.Fatalf("submit: %v", err)
	}
	return tasks
}

func status(t *testing.T, pool *pgxpool.Pool, task TaskID) TaskStatus {
	t.Helper()
	var s string
	if err := pool.QueryRow(t.Context(), "select status from tasks where id = $1", uuid.UUID(task)).Scan(&s); err != nil {
		t.Fatal(err)
	}
	return TaskStatus(s)
}
