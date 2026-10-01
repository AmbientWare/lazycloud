package schedules_test

import (
	"errors"
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
	spec := apitypes.FunctionSpec{
		Name: "nightly", Handler: new("app:nightly"), Source: apitypes.SourceRef{Sha256: source},
		Image:     apitypes.ImageSpec{PythonVersion: apitypes.N312},
		Resources: apitypes.Resources{CpuMillis: 1000, MemoryMib: 512},
		Cron:      cron, MaxPendingTasks: &maxPending,
	}
	if _, err := f.control.Deploy(t.Context(), f.workspace, "reports", apitypes.DeploymentRequest{Functions: []apitypes.FunctionSpec{spec}}); err != nil {
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

func ptr(s string) *string { return &s }

func TestDeployNormalizesAndReplacesTheSchedule(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	f.deploy(t, ptr("Every 5m"), 10)
	first := f.schedule(t)
	if first == nil || first.Expression != "*/5 * * * *" || !first.NextRunAt.After(time.Now()) {
		t.Fatalf("schedule after deploy: %+v", first)
	}
	fn, err := f.control.GetFunction(t.Context(), f.workspace, "reports", "nightly")
	if err != nil {
		t.Fatal(err)
	}
	if *fn.ActiveRelease.Spec.KeepWarmSeconds != 0 {
		t.Fatalf("a scheduled function keeps containers warm for %d s", *fn.ActiveRelease.Spec.KeepWarmSeconds)
	}

	f.deploy(t, ptr("@hourly"), 10)
	if s := f.schedule(t); s.Expression != "@hourly" || s.NextRunAt.Minute() != 0 {
		t.Fatalf("replaced schedule: %+v", s)
	}
	// Removing cron keeps the function but ends its schedule.
	f.deploy(t, nil, 10)
	if s := f.schedule(t); s != nil {
		t.Fatalf("schedule after removing cron: %+v", s)
	}

	var invalid *control.InvalidSpecError
	_, err = f.control.Deploy(t.Context(), f.workspace, "reports", apitypes.DeploymentRequest{Functions: []apitypes.FunctionSpec{{
		Name: "bad", Handler: new("app:bad"), Source: apitypes.SourceRef{Sha256: source},
		Image: apitypes.ImageSpec{PythonVersion: apitypes.N312}, Resources: apitypes.Resources{CpuMillis: 1000, MemoryMib: 512},
		Cron: ptr("every 60m"),
	}}})
	if !errors.As(err, &invalid) || !strings.Contains(err.Error(), "unsupported cron interval: every 60m") {
		t.Fatalf("invalid cron deploy = %v", err)
	}
}

func TestMissedOccurrencesCollapseIntoOneTask(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	f.deploy(t, ptr("every 1m"), 10)
	missed := f.due(t, 10*time.Minute)

	result, err := f.schedules.Fire(t.Context(), slog.New(slog.DiscardHandler))
	if err != nil {
		t.Fatal(err)
	}
	if result.Admitted != 1 || f.scheduledTasks(t) != 1 {
		t.Fatalf("fire admitted %d, %d scheduled tasks", result.Admitted, f.scheduledTasks(t))
	}
	s := f.schedule(t)
	if !s.LastRunAt.Equal(missed) || s.LastTask == nil || !s.NextRunAt.After(time.Now()) {
		t.Fatalf("schedule after firing: %+v", s)
	}
	var scheduledFor time.Time
	if err := f.pool.QueryRow(t.Context(), "select scheduled_for from tasks where id = $1", *s.LastTask).Scan(&scheduledFor); err != nil {
		t.Fatal(err)
	}
	if !scheduledFor.Equal(missed) {
		t.Fatalf("task scheduled for %s, want %s", scheduledFor, missed)
	}
	if result, _ := f.schedules.Fire(t.Context(), slog.New(slog.DiscardHandler)); result.Admitted != 0 {
		t.Fatalf("a second pass admitted %d", result.Admitted)
	}
}

func TestConcurrentSchedulersAdmitAnOccurrenceOnce(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	f.deploy(t, ptr("every 1m"), 100)
	f.due(t, time.Minute)

	var wg sync.WaitGroup
	admitted := make([]int, 8)
	for n := range admitted {
		wg.Go(func() {
			result, err := f.schedules.Fire(t.Context(), slog.New(slog.DiscardHandler))
			if err != nil {
				t.Error(err)
			}
			admitted[n] = result.Admitted
		})
	}
	wg.Wait()
	total := 0
	for _, n := range admitted {
		total += n
	}
	if total != 1 || f.scheduledTasks(t) != 1 {
		t.Fatalf("%d schedulers admitted %d tasks, %d stored", len(admitted), total, f.scheduledTasks(t))
	}
}

func TestRejectedOccurrencesAdvanceWithTheirReason(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	f.deploy(t, ptr("every 1m"), 1)
	// One queued task fills max_pending_tasks.
	f.due(t, 2*time.Minute)
	if result, err := f.schedules.Fire(t.Context(), slog.New(slog.DiscardHandler)); err != nil || result.Admitted != 1 {
		t.Fatalf("first occurrence: %+v, %v", result, err)
	}
	f.due(t, time.Minute)
	result, err := f.schedules.Fire(t.Context(), slog.New(slog.DiscardHandler))
	if err != nil || result.Skipped != 1 {
		t.Fatalf("second occurrence: %+v, %v", result, err)
	}
	s := f.schedule(t)
	if s.LastError == nil || !strings.Contains(*s.LastError, "max_pending_tasks") || !s.NextRunAt.After(time.Now()) {
		t.Fatalf("rejected occurrence: %+v", s)
	}

	// A paused app admits nothing and the schedule keeps advancing.
	if _, err := f.pool.Exec(t.Context(), "update apps set state = 'paused'"); err != nil {
		t.Fatal(err)
	}
	f.due(t, time.Minute)
	if result, _ := f.schedules.Fire(t.Context(), slog.New(slog.DiscardHandler)); result.Skipped != 1 {
		t.Fatalf("paused app: %+v", result)
	}
	if s := f.schedule(t); !strings.Contains(*s.LastError, "paused") {
		t.Fatalf("paused app reason %q", *s.LastError)
	}
}
