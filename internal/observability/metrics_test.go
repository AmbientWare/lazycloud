package observability_test

import (
	"errors"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/observability"
)

func sample(host compute.HostID, container execution.ContainerID, at time.Time, cpuUsec, rss uint64) observability.MetricSample {
	return observability.MetricSample{
		Host: host, Container: container, ReceivedAt: at, IntervalMs: 5000,
		CPUUsageUsec: cpuUsec, MemoryRSS: rss, NetworkRx: 100, NetworkTx: 50, DiskRead: 10, DiskWrite: 20,
	}
}

// Samples count only from the host the container is assigned to, and
// points report CPU as millicores over the time each covers.
func TestMetricSamplesComeOnlyFromTheAssignedHost(t *testing.T) {
	f := newFixture(t, `{}`)
	host, container := f.placedContainer(f.release)
	stranger, _ := f.placedContainer(f.release)
	now := time.Now()
	f.exec1("update containers set created_at = $1 where id = $2", now.Add(-time.Minute), uuid.UUID(container))
	stored, err := f.obs.StoreSamples(t.Context(), []observability.MetricSample{
		sample(host, container, now.Add(-10*time.Second), 2_500_000, 64<<20),
		sample(host, container, now.Add(-5*time.Second), 5_000_000, 96<<20),
		sample(stranger, container, now, 9_000_000, 1<<30),
	})
	if err != nil || stored != 2 {
		t.Fatalf("stored %d: %v", stored, err)
	}
	metrics, err := f.obs.ContainerMetrics(t.Context(), f.workspace, container, observability.MetricsQuery{})
	if err != nil {
		t.Fatal(err)
	}
	if metrics.StepSeconds != 5 || metrics.CpuTotalMillicores != 1000 || metrics.MemoryTotalBytes != 1<<28 || len(metrics.Points) != 2 {
		t.Fatalf("metrics %+v", metrics)
	}
	if p := metrics.Points[1]; p.CpuMillicores != 1000 || p.MemoryRssBytes != 96<<20 || p.NetworkRecvBytes != 100 || p.GpuUtilizationPct != nil {
		t.Fatalf("second point %+v", p)
	}
	// A sample older than the rollup watermark is refused, not lost later.
	f.exec1("update container_metric_rollup set rolled_through = $1", now.Add(time.Minute))
	if late, err := f.obs.StoreSamples(t.Context(), []observability.MetricSample{sample(host, container, now, 1, 1)}); err != nil || late != 0 {
		t.Fatalf("late sample stored %d: %v", late, err)
	}
	if _, err := f.obs.ContainerMetrics(t.Context(), f.workspace, execution.ContainerID(uuid.New()), observability.MetricsQuery{}); !errors.Is(err, observability.ErrNotFound) {
		t.Fatalf("unknown container: %v", err)
	}
}

// The rollup folds whole minutes once, a minute step reads the folded
// points plus the samples not yet folded, and retention deletes only what
// was folded.
func TestRollupFoldsMinutesAndKeepsWhatIsNotFolded(t *testing.T) {
	f := newFixture(t, `{}`)
	host, container := f.placedContainer(f.release)
	minute := time.Now().Truncate(time.Minute).Add(-20 * time.Minute)
	f.exec1("update container_metric_rollup set rolled_through = $1", minute)
	var samples []observability.MetricSample
	// Twelve 5-second samples per minute for 20 minutes, then one more
	// recent sample.
	for i := range 240 {
		samples = append(samples, sample(host, container, minute.Add(time.Duration(i)*5*time.Second), 1_000_000, uint64(i)<<20))
	}
	samples = append(samples, sample(host, container, time.Now(), 4_000_000, 1<<20))
	if _, err := f.obs.StoreSamples(t.Context(), samples); err != nil {
		t.Fatal(err)
	}

	first, err := f.obs.RollUp(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	if first.Folded != 10 || !first.Behind || first.DeletedSamples != 0 {
		t.Fatalf("first pass %+v", first)
	}
	second, err := f.obs.RollUp(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	// The window stops two minutes back, so 18 of 20 minutes are folded.
	if second.Folded != 8 || second.Behind {
		t.Fatalf("second pass %+v", second)
	}

	step := time.Minute
	start := minute
	metrics, err := f.obs.ContainerMetrics(t.Context(), f.workspace, container, observability.MetricsQuery{Start: &start, Step: &step})
	if err != nil {
		t.Fatal(err)
	}
	if metrics.StepSeconds != 60 || len(metrics.Points) != 21 {
		t.Fatalf("%d minute points, step %d", len(metrics.Points), metrics.StepSeconds)
	}
	for i, p := range metrics.Points[:20] {
		if p.IntervalMs != 60_000 || p.CpuMillicores != 200 || p.MemoryRssBytes != int64(12*i+11)<<20 || p.NetworkRecvBytes != 1200 {
			t.Fatalf("minute %d: %+v", i, p)
		}
	}

	// Samples older than an hour go only once folded.
	f.exec1("update container_metric_samples set sampled_at = sampled_at - interval '2 hours'")
	f.exec1("update container_metric_rollup set rolled_through = $1", minute.Add(-2*time.Hour+5*time.Minute))
	pruned, err := f.obs.RollUp(t.Context())
	if err != nil {
		t.Fatal(err)
	}
	var left int
	if err := f.pool.QueryRow(t.Context(), "select count(*) from container_metric_samples").Scan(&left); err != nil {
		t.Fatal(err)
	}
	// The pass folds ten more minutes, to 15 minutes into the samples, then
	// deletes the 180 samples before that.
	if pruned.Folded != 10 || pruned.DeletedSamples != 180 || left != 241-180 {
		t.Fatalf("retention %+v, %d samples left", pruned, left)
	}
}

// A container's lifecycle combines its durable transitions with the stages
// its host reported, which only that host may report, once.
func TestLifecycleCombinesTransitionsAndHostStages(t *testing.T) {
	f := newFixture(t, `{}`)
	host, container := f.placedContainer(f.release)
	stranger, _ := f.placedContainer(f.release)
	assigned := time.Now().Add(-3 * time.Second)
	f.exec1("update containers set created_at = $1, assigned_at = $2 where id = $3",
		assigned.Add(-time.Second), assigned, uuid.UUID(container))
	stages := []observability.StartupStage{
		{Kind: observability.StageImage, StartedAt: assigned, FinishedAt: assigned.Add(time.Second), Cached: true},
		{Kind: observability.StageSource, StartedAt: assigned.Add(time.Second), FinishedAt: assigned.Add(1500 * time.Millisecond)},
	}
	if err := f.obs.RecordStartup(t.Context(), stranger, container, stages); err != nil {
		t.Fatal(err)
	}
	if err := f.obs.RecordStartup(t.Context(), host, container, stages[:1]); err != nil {
		t.Fatal(err)
	}
	restated := append([]observability.StartupStage{{Kind: observability.StageImage, StartedAt: assigned, FinishedAt: assigned.Add(time.Hour)}}, stages[1:]...)
	if err := f.obs.RecordStartup(t.Context(), host, container, restated); err != nil {
		t.Fatal(err)
	}
	if _, err := f.exec.StopContainer(t.Context(), f.workspace, container); err != nil {
		t.Fatal(err)
	}

	lifecycle, err := f.obs.ContainerLifecycle(t.Context(), f.workspace, container)
	if err != nil {
		t.Fatal(err)
	}
	var got []apitypes.LifecycleStageKind
	for _, s := range lifecycle.Stages {
		got = append(got, s.Stage)
	}
	want := []apitypes.LifecycleStageKind{
		apitypes.LifecycleStageKindPlacement, apitypes.LifecycleStageKindImage,
		apitypes.LifecycleStageKindSource, apitypes.LifecycleStageKindDraining,
	}
	if len(got) != len(want) {
		t.Fatalf("stages %v", got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("stages %v", got)
		}
	}
	image := lifecycle.Stages[1]
	if *image.DurationMs != 1000 || image.Cached == nil || !*image.Cached || *lifecycle.Stages[0].DurationMs != 1000 {
		t.Fatalf("image stage %+v, placement %+v", image, lifecycle.Stages[0])
	}
	if draining := lifecycle.Stages[3]; draining.FinishedAt != nil || lifecycle.State != apitypes.ContainerStateDraining || *lifecycle.Host != "h1" {
		t.Fatalf("draining lifecycle %+v", lifecycle)
	}
	otherWS, _, _, _ := f.addFunction("other", "reports", "summarize", `{}`)
	if _, err := f.obs.ContainerLifecycle(t.Context(), otherWS, container); !errors.Is(err, observability.ErrNotFound) {
		t.Fatalf("another workspace read the lifecycle: %v", err)
	}
}
