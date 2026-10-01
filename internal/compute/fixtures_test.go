package compute_test

import (
	"log/slog"
	"regexp"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/scheduling"
)

const gib = int64(1) << 30

func discard() *slog.Logger { return slog.New(slog.DiscardHandler) }

// owners are the real compute, execution and scheduling owners over one
// test database.
type owners struct {
	pool       *pgxpool.Pool
	compute    *compute.Compute
	execution  *execution.Execution
	scheduling *scheduling.Scheduling
}

func newOwners(t *testing.T, config compute.Config) owners {
	t.Helper()
	pool := dbtest.New(t)
	e := execution.NewExecution(pool)
	return owners{
		pool: pool, compute: compute.NewCompute(pool, e, config), execution: e,
		scheduling: scheduling.NewScheduling(pool, discard()),
	}
}

func run(t *testing.T, pool *pgxpool.Pool, sql string, args ...any) {
	t.Helper()
	if _, err := pool.Exec(t.Context(), sql, args...); err != nil {
		t.Fatalf("%s: %v", sql, err)
	}
}

func scan[T any](t *testing.T, pool *pgxpool.Pool, sql string, args ...any) T {
	t.Helper()
	var v T
	if err := pool.QueryRow(t.Context(), sql, args...).Scan(&v); err != nil {
		t.Fatalf("%s: %v", sql, err)
	}
	return v
}

func newUser(t *testing.T, pool *pgxpool.Pool, email string) identity.UserID {
	t.Helper()
	return identity.UserID(scan[uuid.UUID](t, pool, "insert into users (email) values ($1) returning id", email))
}

// newWorkspace inserts a workspace owned by owner.
func newWorkspace(t *testing.T, pool *pgxpool.Pool, name string, owner identity.UserID) uuid.UUID {
	t.Helper()
	id := scan[uuid.UUID](t, pool, "insert into workspaces (name) values ($1) returning id", name)
	run(t, pool, "insert into workspace_members (workspace_id, user_id, role) values ($1, $2, 'owner')", id, uuid.UUID(owner))
	return id
}

// newRelease inserts an active function in workspace whose active release
// has spec, and returns the release.
func newRelease(t *testing.T, pool *pgxpool.Pool, workspace uuid.UUID, spec string) uuid.UUID {
	t.Helper()
	return deploy(t, pool, workspace, "app_"+uuid.NewString()[:8], spec)
}

// deploy inserts the active function app.f in workspace whose active
// release has spec, and returns the release.
func deploy(t *testing.T, pool *pgxpool.Pool, workspace uuid.UUID, app, spec string) uuid.UUID {
	t.Helper()
	release := scan[uuid.UUID](t, pool, `
with app as (insert into apps (workspace_id, name, state) values ($1, $2, 'active') returning id),
     wl as (insert into workloads (app_id, kind, name, desired_state) select id, 'function', 'f', 'active' from app returning id),
     rel as (insert into releases (workload_id, version, spec, spec_digest, source_sha256)
             select id, 1, $3::jsonb, sha256('spec'), sha256('src') from wl returning id, workload_id)
select id from rel`, workspace, app, spec)
	run(t, pool, "update workloads set active_release_id = $1, next_version = 2 where id = (select workload_id from releases where id = $1)", release)
	return release
}

func pendingContainer(t *testing.T, pool *pgxpool.Pool, workspace, release uuid.UUID, cpu, memory int64) uuid.UUID {
	t.Helper()
	return scan[uuid.UUID](t, pool, `
insert into containers (workspace_id, release_id, state, slots, cpu_millis, memory_bytes)
values ($1, $2, 'pending', 1, $3, $4) returning id`, workspace, release, cpu, memory)
}

// hostSpec is a host row; zero fields take the column defaults of an online,
// ready, available platform host run by an operator's agent.
type hostSpec struct {
	Kind       compute.HostKind
	Provider   compute.Provider
	Phase      compute.Phase
	CPU        int64
	Memory     int64
	GPUType    string
	GPUCount   int
	Market     compute.Market
	Region     string
	Zone       string
	ZoneID     string
	InstanceID string
	// Unenrolled hosts have no token and have never reported.
	Unenrolled bool
}

func newHost(t *testing.T, pool *pgxpool.Pool, h hostSpec) compute.HostID {
	t.Helper()
	kind, provider, phase := h.Kind, h.Provider, h.Phase
	if kind == "" {
		kind = compute.KindPlatform
	}
	if provider == "" {
		provider = compute.ProviderAgent
	}
	if phase == "" {
		phase = compute.PhaseReady
	}
	if h.CPU == 0 {
		h.CPU, h.Memory = 4000, 8*gib
	}
	var market, instance *string
	if h.Market != "" {
		m := string(h.Market)
		market = &m
	}
	if h.InstanceID != "" {
		instance = &h.InstanceID
	}
	return compute.HostID(scan[uuid.UUID](t, pool, `
insert into hosts (name, token_hash, state, last_seen_at, kind, provider, phase, cpu_millis, memory_bytes, gpu_type, gpu_count,
                   market, region, availability_zone, availability_zone_id, instance_id, launched_at)
values ('h', case when $13 then null else sha256(random()::text::bytea) end,
        case when $13 then 'offline' else 'online' end, case when $13 then null else now() end, $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
        case when $12::text is null then null else now() end)
returning id`, string(kind), string(provider), string(phase), h.CPU, h.Memory, h.GPUType, h.GPUCount, market,
		h.Region, h.Zone, h.ZoneID, instance, h.Unenrolled))
}

// work is a running attempt of a task on a ready container.
type work struct {
	container uuid.UUID
	task      uuid.UUID
	attempt   uuid.UUID
}

// runningAttempt puts a ready container of release on host running one
// attempt of a task that may try twice.
func runningAttempt(t *testing.T, pool *pgxpool.Pool, workspace, release uuid.UUID, host compute.HostID) work {
	t.Helper()
	var w work
	err := pool.QueryRow(t.Context(), `
with ctr as (insert into containers (workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, assigned_at, ready_at)
             values ($1, $2, 'ready', $3, 1, 1000, 1 << 28, now(), now()) returning id),
     task as (insert into tasks (workspace_id, workload_id, release_id, status, attempt_count, max_attempts, started_at)
              select $1, r.workload_id, r.id, 'running', 1, 2, now() from releases r where r.id = $2 returning id),
     att as (insert into attempts (task_id, number, container_id, state, deadline_at)
             select task.id, 1, ctr.id, 'running', now() + interval '1 hour' from task, ctr returning id)
select ctr.id, task.id, att.id from ctr, task, att`, workspace, release, uuid.UUID(host)).Scan(&w.container, &w.task, &w.attempt)
	if err != nil {
		t.Fatalf("insert running attempt: %v", err)
	}
	run(t, pool, "update tasks set current_attempt_id = $1 where id = $2", w.attempt, w.task)
	return w
}

// assertRetried checks that w's container stopped as host_lost and its
// attempt was lost and queued for a retry.
func assertRetried(t *testing.T, pool *pgxpool.Pool, w work) {
	t.Helper()
	var container, reason, attempt, task string
	err := pool.QueryRow(t.Context(), `
select c.state, coalesce(c.stop_reason, ''), a.state, t.status
from attempts a join containers c on c.id = a.container_id join tasks t on t.id = a.task_id
where a.id = $1`, w.attempt).Scan(&container, &reason, &attempt, &task)
	if err != nil {
		t.Fatal(err)
	}
	if container != "stopped" || reason != "host_lost" || attempt != "lost" || task != "queued" {
		t.Fatalf("container %s (%s), attempt %s, task %s; want stopped (host_lost), lost, queued for a retry", container, reason, attempt, task)
	}
}

func hostPhase(t *testing.T, pool *pgxpool.Pool, host compute.HostID) (phase string, failure *string) {
	t.Helper()
	if err := pool.QueryRow(t.Context(), "select phase, failure from hosts where id = $1", uuid.UUID(host)).Scan(&phase, &failure); err != nil {
		t.Fatal(err)
	}
	return phase, failure
}

func containerHost(t *testing.T, pool *pgxpool.Pool, container uuid.UUID) *uuid.UUID {
	t.Helper()
	return scan[*uuid.UUID](t, pool, "select host_id from containers where id = $1", container)
}

func place(t *testing.T, o owners) int {
	t.Helper()
	result, err := o.scheduling.Place(t.Context())
	if err != nil {
		t.Fatalf("place: %v", err)
	}
	return result.Assigned
}

// publish makes agent 1.0.0 the target release.
func publish(t *testing.T, c *compute.Compute) compute.AgentRelease {
	t.Helper()
	release := compute.AgentRelease{Version: "1.0.0", SHA256: map[string]string{
		"amd64": "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1",
		"arm64": "b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2",
	}}
	if err := c.PublishAgentRelease(t.Context(), release); err != nil {
		t.Fatal(err)
	}
	return release
}

var joinTokenPattern = regexp.MustCompile(`--join-token '([^']+)'`) //nolint:gochecknoglobals // Compiled once.

// joinToken reads the join token out of a join command.
func joinToken(t *testing.T, cmd compute.JoinCommand) string {
	t.Helper()
	m := joinTokenPattern.FindStringSubmatch(cmd.Command)
	if m == nil {
		t.Fatalf("join command has no join token: %s", cmd.Command)
	}
	return m[1]
}
