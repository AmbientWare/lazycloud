package observability_test

import (
	"errors"
	"math"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

// task inserts a task of release submitted at created whose ids carry the
// same time, as uuidv7() ids do. A started task ran for run.
func (f *fixture) task(workspace identity.WorkspaceID, workload, release uuid.UUID, status string, created time.Time, startup, run time.Duration) {
	f.t.Helper()
	var started, finished *time.Time
	if startup >= 0 {
		s := created.Add(startup)
		started = &s
		if run >= 0 {
			e := s.Add(run)
			finished = &e
		}
	}
	f.exec1(`insert into tasks (id, workspace_id, workload_id, release_id, status, max_attempts, created_at, started_at, finished_at)
values (uuidv7($5::timestamptz - now()), $1, $2, $3, $4, 1, $5, $6, $7)`,
		uuid.UUID(workspace), workload, release, status, created, started, finished)
}

func (f *fixture) container(release uuid.UUID, state string, created time.Time, assigned *time.Time, stopped *time.Time, cpuMillis int64) {
	f.t.Helper()
	// A placed container that has not stopped needs a host.
	f.exec1(`with host as (insert into hosts (name, token_hash, state, cpu_millis, memory_bytes)
                     values ('h', sha256(gen_random_uuid()::text::bytea), 'online', 64000, 1 << 40) returning id)
insert into containers (id, workspace_id, release_id, state, host_id, slots, cpu_millis, memory_bytes, created_at, assigned_at, stopped_at, stop_reason)
select uuidv7($3::timestamptz - now()), a.workspace_id, $1, $2,
       case when $2 not in ('pending', 'stopped') then host.id end, 1, $6, 1 << 30, $3, $4, $5,
       case when $2 = 'stopped' then 'stopped' end
from host, releases r join workloads w on w.id = r.workload_id join apps a on a.id = w.app_id where r.id = $1`,
		release, state, created, assigned, stopped, cpuMillis)
}

const none = time.Duration(-1)

// Durations are percentiles over finished tasks per submission bucket;
// cold starts are the deployment's containers created in the bucket. Tasks
// of other deployments or outside the range do not count.
func TestWorkloadPerformanceBucketsLatencyAndColdStarts(t *testing.T) {
	f := newFixture(t, `{}`)
	_, _, otherWL, otherRel := f.addFunction("acme", "billing", "charge", `{}`)
	hour := time.Now().Truncate(time.Hour)
	early := hour.Add(-2 * time.Hour).Add(10 * time.Minute)
	for _, run := range []time.Duration{100, 200, 300} {
		f.task(f.workspace, f.workload, f.release, "succeeded", early, 50*time.Millisecond, run*time.Millisecond)
	}
	f.task(f.workspace, f.workload, f.release, "failed", early, 50*time.Millisecond, time.Second)
	f.task(f.workspace, f.workload, f.release, "queued", hour.Add(-30*time.Minute), none, none)
	f.task(f.workspace, f.workload, f.release, "running", hour.Add(-30*time.Minute), time.Second, none)
	f.task(f.workspace, f.workload, f.release, "succeeded", hour.Add(-72*time.Hour), 0, time.Hour)
	f.task(f.workspace, otherWL, otherRel, "succeeded", early, 0, time.Hour)
	f.container(f.release, "ready", early, &early, nil, 1000)
	f.container(f.release, "stopped", early, &early, &early, 1000)
	f.container(otherRel, "ready", early, &early, nil, 1000)

	perf, err := f.obs.WorkloadPerformance(t.Context(), f.workspace, f.workload, observability.RangeQuery{})
	if err != nil {
		t.Fatal(err)
	}
	if len(perf.Buckets) != 2 || perf.WindowSeconds != 3600 {
		t.Fatalf("buckets %+v", perf)
	}
	a, b := perf.Buckets[0], perf.Buckets[1]
	if !a.Timestamp.Equal(hour.Add(-2*time.Hour)) || a.Count != 4 || a.ColdStarts != 2 ||
		math.Abs(*a.P50Ms-250) > 0.01 || math.Abs(*a.P95Ms-895) > 0.01 ||
		a.StatusCounts.Succeeded != 3 || a.StatusCounts.Failed != 1 {
		t.Fatalf("first bucket %+v p50 %v p95 %v", a, *a.P50Ms, *a.P95Ms)
	}
	if b.Count != 0 || b.P50Ms != nil || b.ColdStarts != 0 || b.StatusCounts.Queued != 1 || b.StatusCounts.Running != 1 {
		t.Fatalf("second bucket %+v", b)
	}
	otherWS, _, _, _ := f.addFunction("other", "reports", "summarize", `{}`)
	if _, err := f.obs.WorkloadPerformance(t.Context(), otherWS, f.workload, observability.RangeQuery{}); !errors.Is(err, observability.ErrNotFound) {
		t.Fatalf("another workspace read the deployment: %v", err)
	}
	start, end := hour.Add(-48*time.Hour), hour
	if _, err := f.obs.WorkloadPerformance(t.Context(), f.workspace, f.workload,
		observability.RangeQuery{Start: &start, End: &end, Width: time.Minute}); err == nil {
		t.Fatal("2,880 one-minute buckets were accepted")
	}
}

// Workspace task metrics and activity cover the tasks submitted in the
// range; activity buckets are dense and split per app, or per function of
// one app.
func TestTaskMetricsAndActivity(t *testing.T) {
	f := newFixture(t, `{}`)
	_, _, chargeWL, chargeRel := f.addFunction("acme", "billing", "charge", `{}`)
	hour := time.Now().Truncate(time.Hour)
	early := hour.Add(-2 * time.Hour).Add(10 * time.Minute)
	for _, run := range []time.Duration{100, 200, 300} {
		f.task(f.workspace, f.workload, f.release, "succeeded", early, 20*time.Millisecond, run*time.Millisecond)
	}
	f.task(f.workspace, f.workload, f.release, "failed", early, 40*time.Millisecond, time.Second)
	f.task(f.workspace, f.workload, f.release, "queued", hour.Add(-30*time.Minute), none, none)
	f.task(f.workspace, chargeWL, chargeRel, "cancelled", hour.Add(-30*time.Minute), none, none)
	f.task(f.workspace, f.workload, f.release, "succeeded", hour.Add(-72*time.Hour), 0, time.Hour)

	m, err := f.obs.TaskMetrics(t.Context(), f.workspace, nil, nil, observability.TaskFilter{})
	if err != nil {
		t.Fatal(err)
	}
	if m.Total != 6 || m.StatusCounts.Failed != 1 || m.StatusCounts.Cancelled != 1 || math.Abs(m.FailureRate-1.0/6) > 1e-9 ||
		math.Abs(*m.RuntimeMsP50-250) > 0.01 || math.Abs(*m.AverageRuntimeMs-400) > 0.01 || math.Abs(*m.StartupMsP50-20) > 0.01 {
		t.Fatalf("metrics %+v", m)
	}
	app, fn := "billing", "charge"
	m, err = f.obs.TaskMetrics(t.Context(), f.workspace, nil, nil, observability.TaskFilter{App: &app, Function: &fn})
	if err != nil {
		t.Fatal(err)
	}
	if m.Total != 1 || m.RuntimeMsP50 != nil || m.StartupMsP50 != nil {
		t.Fatalf("billing metrics %+v", m)
	}

	activity, err := f.obs.TaskActivity(t.Context(), f.workspace, observability.RangeQuery{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(activity.Series) != 2 || activity.Series[0].App != "reports" || activity.Series[0].Total != 5 ||
		len(activity.Series[0].Buckets) != 24 || activity.Series[1].Total != 1 {
		t.Fatalf("activity %+v", activity)
	}
	reports := activity.Series[0].Buckets
	if last := reports[23]; !last.Timestamp.Equal(hour) {
		t.Fatalf("the last bucket starts at %v, want the current hour %v", last.Timestamp, hour)
	}
	if c := reports[21].StatusCounts; c.Succeeded != 3 || c.Failed != 1 {
		t.Fatalf("bucket two hours back %+v", c)
	}
	byFunction, err := f.obs.TaskActivity(t.Context(), f.workspace, observability.RangeQuery{}, &app)
	if err != nil {
		t.Fatal(err)
	}
	if len(byFunction.Series) != 1 || *byFunction.Series[0].Function != "charge" || byFunction.Series[0].Buckets[22].StatusCounts.Cancelled != 1 {
		t.Fatalf("billing activity %+v", byFunction)
	}
}

// Account metrics count containers in every member workspace and
// concurrency in the owned ones; activity measures starts per app and the
// average CPU live containers reserved per bucket.
func TestAccountMetricsAndActivity(t *testing.T) {
	f := newFixture(t, `{}`)
	sharedWS, _, _, sharedRel := f.addFunction("shared", "etl", "load", `{}`)
	_, _, _, chargeRel := f.addFunction("acme", "billing", "charge", `{}`)
	hour := time.Now().Truncate(time.Hour)
	assigned, stopped := hour.Add(-3*time.Hour+30*time.Minute), hour.Add(-time.Hour)
	f.container(f.release, "stopped", assigned, &assigned, &stopped, 1000)
	f.container(f.release, "ready", hour.Add(-time.Minute), &hour, nil, 2000)
	f.container(f.release, "pending", hour, nil, nil, 1000)
	f.container(chargeRel, "starting", hour, &hour, nil, 1000)
	f.container(sharedRel, "ready", hour, &hour, nil, 1000)
	member := []identity.Workspace{
		{ID: f.workspace, Name: "acme", Role: identity.RoleOwner},
		{ID: sharedWS, Name: "shared", Role: identity.RoleMember},
	}

	metrics, err := f.obs.AccountMetrics(t.Context(), identity.UserID(uuid.New()), member)
	if err != nil {
		t.Fatal(err)
	}
	if metrics.Containers.Pending != 2 || metrics.Containers.Running != 2 ||
		metrics.Concurrency.CpuContainers != 3 || metrics.Concurrency.Limits != nil {
		t.Fatalf("account metrics %+v", metrics)
	}

	start := hour.Add(-3 * time.Hour)
	cpu, err := f.obs.AccountActivity(t.Context(), member, observability.ActivityQuery{
		RangeQuery: observability.RangeQuery{Start: &start}, Measure: apitypes.ActivityMeasureCpu, Limit: 5,
	})
	if err != nil {
		t.Fatal(err)
	}
	if cpu.Unit != apitypes.Cores || len(cpu.Series) != 3 {
		t.Fatalf("cpu activity %+v", cpu)
	}
	reports := cpu.Series[0]
	if *reports.App != "reports" || *reports.Workspace != "acme" {
		t.Fatalf("largest series %+v", reports)
	}
	if b := reports.Buckets; math.Abs(b[0].Value-0.5) > 1e-6 || math.Abs(b[1].Value-1) > 1e-6 || b[2].Value != 0 {
		t.Fatalf("reports cpu buckets %+v", b)
	}

	starts, err := f.obs.AccountActivity(t.Context(), member, observability.ActivityQuery{
		RangeQuery: observability.RangeQuery{Start: &start}, Measure: apitypes.ActivityMeasureContainers, Limit: 1,
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(starts.Series) != 2 || starts.Series[0].Total != 3 || starts.Series[1].Kind != apitypes.ActivitySeriesKindOther ||
		starts.Series[1].Total != 2 || starts.Total != 5 || starts.Unit != apitypes.Starts {
		t.Fatalf("container starts %+v", starts)
	}
}
