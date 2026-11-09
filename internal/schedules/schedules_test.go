package schedules_test

import (
	"log/slog"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/database/dbtest"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/schedules"
)

var source = strings.Repeat("ab", 32) //nolint:gochecknoglobals // test constant

type fixture struct {
	pool      *pgxpool.Pool
	workspace identity.WorkspaceID
	control   *control.Control
	schedules *schedules.Schedules
}

func newFixture(t *testing.T) fixture {
	t.Helper()
	pool := dbtest.New(t)
	var ws uuid.UUID
	err := pool.QueryRow(t.Context(), `
with ws as (insert into workspaces (name) values ('ws') returning id),
     src as (insert into source_objects (workspace_id, sha256, size_bytes) select id, decode($1, 'hex'), 10 from ws)
select id from ws`, source).Scan(&ws)
	if err != nil {
		t.Fatal(err)
	}
	dbtest.OwnWorkspaces(t, pool)
	return fixture{
		pool: pool, workspace: identity.WorkspaceID(ws), control: control.NewControl(pool),
		schedules: schedules.NewSchedules(pool, execution.NewExecution(pool)),
	}
}

func (f fixture) deploy(t *testing.T, cron *string, maxPending int) {
	t.Helper()
	spec := apitypes.WorkloadSpec{
		Kind: apitypes.WorkloadKindFunction, Name: "nightly", Handler: new("app:nightly"), Source: apitypes.SourceRef{Sha256: source},
		Image:     apitypes.ImageSpec{PythonVersion: apitypes.N312},
		Resources: apitypes.Resources{CpuMillis: 1000, MemoryMib: 512},
		Cron:      cron, MaxPendingTasks: &maxPending,
	}
	if _, err := f.control.Deploy(t.Context(), f.workspace, "reports", apitypes.DeploymentRequest{Workloads: []apitypes.WorkloadSpec{spec}}); err != nil {
		t.Fatal(err)
	}
}

func (f fixture) schedule(t *testing.T) *schedules.Schedule {
	t.Helper()
	s, err := f.schedules.ForFunction(t.Context(), f.workspace, "reports", "nightly")
	if err != nil {
		t.Fatal(err)
	}
	return s
}

// due moves the schedule's next occurrence into the past.
func (f fixture) due(t *testing.T, ago time.Duration) time.Time {
	t.Helper()
	at := time.Now().Add(-ago).UTC().Truncate(time.Minute)
	if _, err := f.pool.Exec(t.Context(), "update schedules set next_fire_at = $1", at); err != nil {
		t.Fatal(err)
	}
	return at
}

func (f fixture) scheduledTasks(t *testing.T) int {
	t.Helper()
	var n int
	if err := f.pool.QueryRow(t.Context(), "select count(*) from tasks where scheduled_for is not null").Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func TestDeployNormalizesAndReplacesTheSchedule(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	deployed := time.Now()
	f.deploy(t, new("Every 5m"), 10)
	first := f.schedule(t)
	// The next occurrence follows the deploy; comparing with a later clock
	// reading fails when the deploy lands just before a boundary.
	if first == nil || first.Expression != "*/5 * * * *" || !first.NextRunAt.After(deployed) {
		t.Fatalf("schedule after deploy: %+v", first)
	}
	id, err := f.control.FindWorkload(t.Context(), f.workspace, control.WorkloadRef{App: "reports", Kind: apitypes.WorkloadKindFunction, Name: "nightly"})
	if err != nil {
		t.Fatal(err)
	}
	release, err := f.control.Release(t.Context(), f.workspace, id, nil)
	if err != nil {
		t.Fatal(err)
	}
	if *release.Spec.KeepWarmSeconds != 0 {
		t.Fatalf("a scheduled function keeps containers warm for %d s", *release.Spec.KeepWarmSeconds)
	}

	f.deploy(t, new("@hourly"), 10)
	if s := f.schedule(t); s.Expression != "@hourly" || s.NextRunAt.Minute() != 0 {
		t.Fatalf("replaced schedule: %+v", s)
	}
	// Removing cron keeps the function but ends its schedule.
	f.deploy(t, nil, 10)
	if s := f.schedule(t); s != nil {
		t.Fatalf("schedule after removing cron: %+v", s)
	}

	_, err = f.control.Deploy(t.Context(), f.workspace, "reports", apitypes.DeploymentRequest{Workloads: []apitypes.WorkloadSpec{{
		Kind: apitypes.WorkloadKindFunction, Name: "bad", Handler: new("app:bad"), Source: apitypes.SourceRef{Sha256: source},
		Image: apitypes.ImageSpec{PythonVersion: apitypes.N312}, Resources: apitypes.Resources{CpuMillis: 1000, MemoryMib: 512},
		Cron: new("every 60m"),
	}}})
	if apitypes.CodeOf(err) != apitypes.InvalidRequest || !strings.Contains(err.Error(), "unsupported cron interval: every 60m") {
		t.Fatalf("invalid cron deploy = %v", err)
	}
}

func TestMissedOccurrencesCollapseIntoOneTask(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	f.deploy(t, new("every 1m"), 10)
	missed := f.due(t, 10*time.Minute)

	fired := time.Now()
	if err := f.schedules.Fire(t.Context(), slog.New(slog.DiscardHandler)); err != nil {
		t.Fatal(err)
	}
	if n := f.scheduledTasks(t); n != 1 {
		t.Fatalf("%d scheduled tasks, want 1", n)
	}
	s := f.schedule(t)
	if !s.LastRunAt.Equal(missed) || s.LastTask == nil || !s.NextRunAt.After(fired) {
		t.Fatalf("schedule after firing: %+v", s)
	}
	var scheduledFor time.Time
	if err := f.pool.QueryRow(t.Context(), "select scheduled_for from tasks where id = $1", *s.LastTask).Scan(&scheduledFor); err != nil {
		t.Fatal(err)
	}
	if !scheduledFor.Equal(missed) {
		t.Fatalf("task scheduled for %s, want %s", scheduledFor, missed)
	}
	if err := f.schedules.Fire(t.Context(), slog.New(slog.DiscardHandler)); err != nil || f.scheduledTasks(t) != 1 {
		t.Fatalf("a second pass: %d scheduled tasks, %v", f.scheduledTasks(t), err)
	}
}

func TestConcurrentSchedulersAdmitAnOccurrenceOnce(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	f.deploy(t, new("every 1m"), 100)
	f.due(t, time.Minute)

	var wg sync.WaitGroup
	for range 8 {
		wg.Go(func() {
			if err := f.schedules.Fire(t.Context(), slog.New(slog.DiscardHandler)); err != nil {
				t.Error(err)
			}
		})
	}
	wg.Wait()
	if n := f.scheduledTasks(t); n != 1 {
		t.Fatalf("8 schedulers stored %d tasks, want 1", n)
	}
}

func TestRejectedOccurrencesAdvanceWithTheirReason(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	f.deploy(t, new("every 1m"), 1)
	// One queued task fills max_pending_tasks.
	f.due(t, 2*time.Minute)
	if err := f.schedules.Fire(t.Context(), slog.New(slog.DiscardHandler)); err != nil || f.scheduledTasks(t) != 1 {
		t.Fatalf("first occurrence: %d scheduled tasks, %v", f.scheduledTasks(t), err)
	}
	f.due(t, time.Minute)
	fired := time.Now()
	if err := f.schedules.Fire(t.Context(), slog.New(slog.DiscardHandler)); err != nil || f.scheduledTasks(t) != 1 {
		t.Fatalf("second occurrence: %d scheduled tasks, %v", f.scheduledTasks(t), err)
	}
	s := f.schedule(t)
	if s.LastError == nil || !strings.Contains(*s.LastError, "max_pending_tasks") || !s.NextRunAt.After(fired) {
		t.Fatalf("rejected occurrence: %+v", s)
	}

	// A paused app admits nothing and the schedule keeps advancing.
	if _, err := f.pool.Exec(t.Context(), "update apps set state = 'paused'"); err != nil {
		t.Fatal(err)
	}
	f.due(t, time.Minute)
	if err := f.schedules.Fire(t.Context(), slog.New(slog.DiscardHandler)); err != nil || f.scheduledTasks(t) != 1 {
		t.Fatalf("paused app: %d scheduled tasks, %v", f.scheduledTasks(t), err)
	}
	if s := f.schedule(t); !strings.Contains(*s.LastError, "paused") {
		t.Fatalf("paused app reason %q", *s.LastError)
	}
}
