package observability

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgtype"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

const (
	sampleStep = 5 * time.Second
	minuteStep = time.Minute
	maxPoints  = 1000
)

// MetricsQuery is an optional range and step.
type MetricsQuery struct {
	Start, End *time.Time
	Step       *time.Duration
}

// ContainerMetrics returns the container's use per step. The range
// defaults to the container's last hour of life. A step under a minute
// reads samples, which exist only for the last hour; longer steps read
// minute points and the samples not yet folded into them.
func (o *Observability) ContainerMetrics(ctx context.Context, ws identity.WorkspaceID, id execution.ContainerID, q MetricsQuery) (apitypes.ContainerMetrics, error) {
	scope, err := o.queries.ContainerMetricsScope(ctx, ContainerMetricsScopeParams{ID: uuid.UUID(id), WorkspaceID: uuid.UUID(ws)})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.ContainerMetrics{}, ErrNotFound
	}
	if err != nil {
		return apitypes.ContainerMetrics{}, fmt.Errorf("read container: %w", err)
	}
	end := scope.ObservedAt
	if scope.StoppedAt != nil {
		end = scope.StoppedAt.Add(sampleStep)
	}
	if q.End != nil {
		end = *q.End
	}
	start := end.Add(-time.Hour)
	if start.Before(scope.CreatedAt) {
		start = scope.CreatedAt
	}
	if q.Start != nil {
		start = *q.Start
	}
	if !start.Before(end) {
		return apitypes.ContainerMetrics{}, fmt.Errorf("%w: start must be before end", ErrInvalidRange)
	}
	if end.Sub(start) > minuteRetention {
		return apitypes.ContainerMetrics{}, fmt.Errorf("%w: metrics are kept 7 days", ErrInvalidRange)
	}
	step := pickStep(start, end, q.Step, scope.ObservedAt)

	var points []pointRow
	if step < minuteStep {
		rows, err := o.queries.SamplePoints(ctx, SamplePointsParams{
			Step: interval(step), ContainerID: uuid.UUID(id), StartAt: start, EndAt: end,
		})
		if err != nil {
			return apitypes.ContainerMetrics{}, fmt.Errorf("read samples: %w", err)
		}
		for _, r := range rows {
			points = append(points, pointRow(r))
		}
	} else {
		folded := scope.RolledThrough
		if folded.After(start) {
			rows, err := o.queries.MinutePoints(ctx, MinutePointsParams{
				Step: interval(step), ContainerID: uuid.UUID(id), StartAt: start, EndAt: minTime(end, folded),
			})
			if err != nil {
				return apitypes.ContainerMetrics{}, fmt.Errorf("read minute points: %w", err)
			}
			for _, r := range rows {
				points = append(points, pointRow(r))
			}
		}
		if end.After(folded) {
			rows, err := o.queries.SamplePoints(ctx, SamplePointsParams{
				Step: interval(step), ContainerID: uuid.UUID(id), StartAt: maxTime(start, folded), EndAt: end,
			})
			if err != nil {
				return apitypes.ContainerMetrics{}, fmt.Errorf("read samples: %w", err)
			}
			for _, r := range rows {
				points = append(points, pointRow(r))
			}
		}
		points = mergePoints(points)
	}
	out := apitypes.ContainerMetrics{
		ContainerId: uuid.UUID(id), CpuTotalMillicores: scope.CpuMillis, MemoryTotalBytes: scope.MemoryBytes,
		StepSeconds: int(step / time.Second), Points: make([]apitypes.ContainerMetricPoint, 0, len(points)),
	}
	for _, p := range points {
		out.Points = append(out.Points, p.out())
	}
	return out, nil
}

// pickStep is the requested step, coarsened to fit maxPoints, to a whole
// minute when samples no longer cover the range, and to a multiple of the
// sampling or minute step.
func pickStep(start, end time.Time, requested *time.Duration, now time.Time) time.Duration {
	step := sampleStep
	if requested != nil {
		step = *requested
	}
	if fit := end.Sub(start) / maxPoints; step < fit {
		step = fit
	}
	if step < minuteStep && start.Before(now.Add(-sampleRetention)) {
		step = minuteStep
	}
	unit := sampleStep
	if step >= minuteStep {
		unit = minuteStep
	}
	if r := step % unit; r != 0 {
		step += unit - r
	}
	return step
}

func interval(d time.Duration) pgtype.Interval {
	return pgtype.Interval{Microseconds: d.Microseconds(), Valid: true}
}

func minTime(a, b time.Time) time.Time {
	if a.Before(b) {
		return a
	}
	return b
}

func maxTime(a, b time.Time) time.Time {
	if a.After(b) {
		return a
	}
	return b
}

// pointRow is one bucket of samples or minute points.
type pointRow struct {
	Bucket              time.Time
	IntervalMs          int64
	CpuUsageUsec        int64
	MemoryRssBytes      int64
	MemorySwapBytes     int64
	NetworkRxBytes      int64
	NetworkTxBytes      int64
	DiskReadBytes       int64
	DiskWriteBytes      int64
	GpuSamples          int32
	GpuUtilizationPct   float64
	GpuMemoryUsedBytes  int64
	GpuMemoryTotalBytes int64
	GpuType             string
}

// mergePoints combines the minute-point and sample halves of a bucket that
// the rollup watermark split.
func mergePoints(points []pointRow) []pointRow {
	sort.SliceStable(points, func(i, j int) bool { return points[i].Bucket.Before(points[j].Bucket) })
	var out []pointRow
	for _, p := range points {
		if n := len(out); n > 0 && out[n-1].Bucket.Equal(p.Bucket) {
			last := &out[n-1]
			if total := last.GpuSamples + p.GpuSamples; total > 0 {
				last.GpuUtilizationPct = (last.GpuUtilizationPct*float64(last.GpuSamples) + p.GpuUtilizationPct*float64(p.GpuSamples)) / float64(total)
				last.GpuSamples = total
			}
			last.IntervalMs += p.IntervalMs
			last.CpuUsageUsec += p.CpuUsageUsec
			last.MemoryRssBytes = max(last.MemoryRssBytes, p.MemoryRssBytes)
			last.MemorySwapBytes = max(last.MemorySwapBytes, p.MemorySwapBytes)
			last.NetworkRxBytes += p.NetworkRxBytes
			last.NetworkTxBytes += p.NetworkTxBytes
			last.DiskReadBytes += p.DiskReadBytes
			last.DiskWriteBytes += p.DiskWriteBytes
			last.GpuMemoryUsedBytes = max(last.GpuMemoryUsedBytes, p.GpuMemoryUsedBytes)
			last.GpuMemoryTotalBytes = max(last.GpuMemoryTotalBytes, p.GpuMemoryTotalBytes)
			if last.GpuType == "" {
				last.GpuType = p.GpuType
			}
			continue
		}
		out = append(out, p)
	}
	return out
}

func (p pointRow) out() apitypes.ContainerMetricPoint {
	out := apitypes.ContainerMetricPoint{
		Timestamp: p.Bucket, IntervalMs: p.IntervalMs,
		MemoryRssBytes: p.MemoryRssBytes, MemorySwapBytes: p.MemorySwapBytes,
		NetworkRecvBytes: p.NetworkRxBytes, NetworkSentBytes: p.NetworkTxBytes,
		DiskReadBytes: p.DiskReadBytes, DiskWriteBytes: p.DiskWriteBytes,
	}
	if p.IntervalMs > 0 {
		// Microseconds of CPU per millisecond is millicores.
		out.CpuMillicores = float64(p.CpuUsageUsec) / float64(p.IntervalMs)
	}
	if p.GpuSamples > 0 {
		util, used, total, kind := p.GpuUtilizationPct, p.GpuMemoryUsedBytes, p.GpuMemoryTotalBytes, p.GpuType
		out.GpuUtilizationPct, out.GpuMemoryUsedBytes, out.GpuMemoryTotalBytes, out.GpuType = &util, &used, &total, &kind
	}
	return out
}

// StartupStageKind names a stage of a container's start on its host.
type StartupStageKind string

const (
	StageImage   StartupStageKind = "image"
	StageSource  StartupStageKind = "source"
	StageCreate  StartupStageKind = "create"
	StageRuntime StartupStageKind = "runtime"
	// StageDisk is leasing and restoring the container's disks.
	StageDisk StartupStageKind = "disk"
)

// StartupStage is how long one stage took.
type StartupStage struct {
	Kind       StartupStageKind
	StartedAt  time.Time
	FinishedAt time.Time
	Cached     bool
}

// RecordStartup stores the stages host reported for container. Only a
// container assigned to host counts, and a stage already stored stays, so
// reports may restate stages freely.
func (o *Observability) RecordStartup(ctx context.Context, host compute.HostID, container execution.ContainerID, stages []StartupStage) error {
	if len(stages) == 0 {
		return nil
	}
	hostID := uuid.UUID(host)
	p := InsertStartupStagesParams{ContainerID: uuid.UUID(container), HostID: &hostID}
	for _, s := range stages {
		p.Stages = append(p.Stages, string(s.Kind))
		p.StartedAt = append(p.StartedAt, s.StartedAt)
		p.FinishedAt = append(p.FinishedAt, s.FinishedAt)
		p.Cached = append(p.Cached, s.Cached)
	}
	if err := o.queries.InsertStartupStages(ctx, p); err != nil {
		return fmt.Errorf("record startup stages: %w", err)
	}
	return nil
}

// maxLifecycles bounds one lifecycle batch.
const maxLifecycles = 200

// ContainerLifecycles returns the lifecycles of the listed function
// containers in ws, in id order; others are left out.
func (o *Observability) ContainerLifecycles(ctx context.Context, ws identity.WorkspaceID, ids []execution.ContainerID) ([]apitypes.ContainerLifecycle, error) {
	if len(ids) > maxLifecycles {
		return nil, fmt.Errorf("%w: at most %d containers", ErrInvalidRange, maxLifecycles)
	}
	raw := make([]uuid.UUID, len(ids))
	for i, id := range ids {
		raw[i] = uuid.UUID(id)
	}
	rows, err := o.queries.ContainerLifecycles(ctx, ContainerLifecyclesParams{WorkspaceID: uuid.UUID(ws), Ids: raw})
	if err != nil {
		return nil, fmt.Errorf("read containers: %w", err)
	}
	if len(rows) == 0 {
		return []apitypes.ContainerLifecycle{}, nil
	}
	found := make([]uuid.UUID, len(rows))
	for i, r := range rows {
		found[i] = r.ID
	}
	stageRows, err := o.queries.StartupStages(ctx, found)
	if err != nil {
		return nil, fmt.Errorf("read startup stages: %w", err)
	}
	stages := map[uuid.UUID][]ContainerStartupStage{}
	for _, s := range stageRows {
		stages[s.ContainerID] = append(stages[s.ContainerID], s)
	}
	out := make([]apitypes.ContainerLifecycle, len(rows))
	for i, r := range rows {
		out[i] = lifecycleOut(r, stages[r.ID])
	}
	return out, nil
}

// ContainerLifecycle returns one container's lifecycle.
func (o *Observability) ContainerLifecycle(ctx context.Context, ws identity.WorkspaceID, id execution.ContainerID) (apitypes.ContainerLifecycle, error) {
	lifecycles, err := o.ContainerLifecycles(ctx, ws, []execution.ContainerID{id})
	if err != nil {
		return apitypes.ContainerLifecycle{}, err
	}
	if len(lifecycles) == 0 {
		return apitypes.ContainerLifecycle{}, ErrNotFound
	}
	return lifecycles[0], nil
}

// lifecycleOut builds the stages from the durable transitions (placement
// and draining) and the stages the host reported.
func lifecycleOut(r ContainerLifecyclesRow, reported []ContainerStartupStage) apitypes.ContainerLifecycle {
	out := apitypes.ContainerLifecycle{
		ContainerId: r.ID, App: &r.AppName, Function: &r.FunctionName,
		State: apitypes.ContainerState(r.State), ExitMessage: r.ExitMessage, Host: r.HostName,
		CreatedAt: r.CreatedAt, AssignedAt: r.AssignedAt, ReadyAt: r.ReadyAt, StoppedAt: r.StoppedAt,
		Stages: []apitypes.LifecycleStage{},
	}
	if r.StopReason != nil {
		reason := apitypes.StopReason(*r.StopReason)
		out.StopReason = &reason
	}
	stage := func(kind apitypes.LifecycleStageKind, started time.Time, finished *time.Time) apitypes.LifecycleStage {
		until := r.ObservedAt
		if finished != nil {
			until = *finished
		}
		ms := until.Sub(started).Milliseconds()
		return apitypes.LifecycleStage{Stage: kind, StartedAt: started, FinishedAt: finished, DurationMs: &ms}
	}
	// Placement ends at assignment, or at the stop of a container that
	// never got a host.
	placed := r.AssignedAt
	if placed == nil {
		placed = r.StoppedAt
	}
	out.Stages = append(out.Stages, stage(apitypes.LifecycleStageKindPlacement, r.CreatedAt, placed))
	for _, s := range reported {
		finished := s.FinishedAt
		st := stage(apitypes.LifecycleStageKind(s.Stage), s.StartedAt, &finished)
		if s.Stage == string(StageImage) {
			cached := s.Cached
			st.Cached = &cached
		}
		out.Stages = append(out.Stages, st)
	}
	if r.DrainStartedAt != nil {
		out.Stages = append(out.Stages, stage(apitypes.LifecycleStageKindDraining, *r.DrainStartedAt, r.StoppedAt))
	}
	sort.SliceStable(out.Stages, func(i, j int) bool { return out.Stages[i].StartedAt.Before(out.Stages[j].StartedAt) })
	return out
}
