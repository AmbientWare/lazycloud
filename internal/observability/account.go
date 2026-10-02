package observability

import (
	"context"
	"fmt"
	"sort"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// AccountMetrics counts live containers in every workspace of member, and
// the concurrency plan limits govern: live containers in the workspaces
// account owns. Containers hold no GPUs yet, so every one counts as a CPU
// container.
func (o *Observability) AccountMetrics(ctx context.Context, account identity.UserID, member []identity.Workspace) (apitypes.AccountMetrics, error) {
	ids := make([]uuid.UUID, len(member))
	owned := map[uuid.UUID]bool{}
	for i, ws := range member {
		ids[i] = uuid.UUID(ws.ID)
		owned[ids[i]] = ws.Role == identity.RoleOwner
	}
	var out apitypes.AccountMetrics
	if len(ids) > 0 {
		rows, err := o.queries.LiveContainerCounts(ctx, ids)
		if err != nil {
			return out, fmt.Errorf("count live containers: %w", err)
		}
		for _, r := range rows {
			switch execution.ContainerState(r.State) {
			case execution.ContainerPending, execution.ContainerStarting:
				out.Containers.Pending += int(r.Containers)
			case execution.ContainerReady, execution.ContainerDraining:
				out.Containers.Running += int(r.Containers)
			case execution.ContainerStopped:
			}
			if owned[r.WorkspaceID] {
				out.Concurrency.CpuContainers += int(r.Containers)
			}
		}
	}
	if o.limits != nil {
		limits, err := o.limits.ConcurrencyLimits(ctx, account)
		if err != nil {
			return out, fmt.Errorf("read plan limits: %w", err)
		}
		if limits != nil {
			out.Concurrency.Limits = &apitypes.ConcurrencyLimits{
				MaxCpuContainers: limits.MaxCPUContainers, MaxGpus: limits.MaxGPUs,
			}
		}
	}
	return out, nil
}

// ActivityQuery selects what account activity measures and how many series
// it keeps before folding the rest into one.
type ActivityQuery struct {
	RangeQuery
	Measure apitypes.ActivityMeasure
	Limit   int
}

// AccountActivity measures member's workspaces per app over time: starts of
// containers or tasks per bucket, or the average CPU cores, GiB of memory or
// GPUs live containers reserved over each bucket.
func (o *Observability) AccountActivity(ctx context.Context, member []identity.Workspace, q ActivityQuery) (apitypes.AccountActivity, error) {
	width := q.width()
	now := time.Now()
	s, err := alignedSpan(q.Start, q.End, width, 24, now)
	if err != nil {
		return apitypes.AccountActivity{}, err
	}
	measure := q.Measure
	if measure == "" {
		measure = apitypes.ActivityMeasureContainers
	}
	ids := make([]uuid.UUID, len(member))
	names := map[uuid.UUID]string{}
	for i, ws := range member {
		ids[i] = uuid.UUID(ws.ID)
		names[ids[i]] = ws.Name
	}
	var rows []activityRow
	var unit apitypes.ActivityUnit
	starts := false
	switch measure {
	case apitypes.ActivityMeasureContainers, apitypes.ActivityMeasureTasks:
		unit, starts = apitypes.Starts, true
		rows, err = o.starts(ctx, measure, ids, s)
	case apitypes.ActivityMeasureCpu:
		unit = apitypes.Cores
		rows, err = o.allocations(ctx, ids, s, 1.0/1000, 0, 0)
	case apitypes.ActivityMeasureMemory:
		unit = apitypes.Gibibytes
		rows, err = o.allocations(ctx, ids, s, 0, 1.0/(1<<30), 0)
	case apitypes.ActivityMeasureGpu:
		unit = apitypes.Gpus
		rows, err = o.allocations(ctx, ids, s, 0, 0, 1)
	default:
		return apitypes.AccountActivity{}, fmt.Errorf("%w: unknown measure %q", ErrInvalidRange, measure)
	}
	if err != nil {
		return apitypes.AccountActivity{}, err
	}
	return buildActivity(rows, names, s, measure, unit, starts, max(q.Limit, 1), now), nil
}

// activityRow is one workspace, app and bucket. AppID is nil for containers
// of no app; Value is a count, or amount-seconds for allocations.
type activityRow struct {
	Workspace uuid.UUID
	AppID     *uuid.UUID
	AppName   *string
	Bucket    time.Time
	Value     float64
}

func (o *Observability) starts(ctx context.Context, measure apitypes.ActivityMeasure, ids []uuid.UUID, s span) ([]activityRow, error) {
	var out []activityRow
	if len(ids) == 0 {
		return out, nil
	}
	if measure == apitypes.ActivityMeasureTasks {
		rows, err := o.queries.TaskStarts(ctx, TaskStartsParams{
			BucketWidth: s.interval(), WorkspaceIds: ids, FromID: s.fromID(), ToID: s.toID(), StartAt: s.start, EndAt: s.end,
		})
		if err != nil {
			return nil, fmt.Errorf("count task starts: %w", err)
		}
		for _, r := range rows {
			appID, name := r.AppID, r.AppName
			out = append(out, activityRow{Workspace: r.WorkspaceID, AppID: &appID, AppName: &name, Bucket: r.Bucket, Value: r.Value})
		}
		return out, nil
	}
	rows, err := o.queries.ContainerStarts(ctx, ContainerStartsParams{
		BucketWidth: s.interval(), WorkspaceIds: ids, FromID: s.fromID(), ToID: s.toID(), StartAt: s.start, EndAt: s.end,
	})
	if err != nil {
		return nil, fmt.Errorf("count container starts: %w", err)
	}
	for _, r := range rows {
		out = append(out, activityRow{Workspace: r.WorkspaceID, AppID: r.AppID, AppName: r.AppName, Bucket: r.Bucket, Value: r.Value})
	}
	return out, nil
}

func (o *Observability) allocations(ctx context.Context, ids []uuid.UUID, s span, perCPUMilli, perMemoryByte, perGPU float64) ([]activityRow, error) {
	var out []activityRow
	if len(ids) == 0 {
		return out, nil
	}
	rows, err := o.queries.Allocations(ctx, AllocationsParams{
		BucketWidth: s.interval(), PerCpuMilli: perCPUMilli, PerMemoryByte: perMemoryByte, PerGpu: perGPU,
		WorkspaceIds: ids, StartAt: s.start, EndAt: s.end,
	})
	if err != nil {
		return nil, fmt.Errorf("read allocations: %w", err)
	}
	for _, r := range rows {
		out = append(out, activityRow{Workspace: r.WorkspaceID, AppID: r.AppID, AppName: r.AppName, Bucket: r.Bucket, Value: r.Value})
	}
	return out, nil
}

// buildActivity makes dense series per workspace and app, sorted by total,
// and folds those past limit into one other series. Allocations divide
// amount-seconds by the seconds each bucket, or the range, has elapsed.
func buildActivity(rows []activityRow, names map[uuid.UUID]string, s span, measure apitypes.ActivityMeasure,
	unit apitypes.ActivityUnit, starts bool, limit int, now time.Time,
) apitypes.AccountActivity {
	buckets := s.buckets()
	index := make(map[time.Time]int, len(buckets))
	for i, b := range buckets {
		index[b] = i
	}
	type key struct {
		workspace uuid.UUID
		app       uuid.UUID
	}
	sums := map[key][]float64{}
	series := map[key]*apitypes.AccountActivitySeries{}
	for _, r := range rows {
		k := key{workspace: r.Workspace}
		if r.AppID != nil {
			k.app = *r.AppID
		}
		if _, ok := series[k]; !ok {
			ws := names[r.Workspace]
			sr := &apitypes.AccountActivitySeries{Kind: apitypes.ActivitySeriesKindUnassigned, Workspace: &ws}
			if r.AppID != nil {
				appID := *r.AppID
				sr.Kind, sr.AppId, sr.App = apitypes.ActivitySeriesKindApp, &appID, r.AppName
			}
			series[k] = sr
			sums[k] = make([]float64, len(buckets))
		}
		if i, ok := index[r.Bucket.UTC()]; ok {
			sums[k][i] += r.Value
		}
	}
	elapsed := func(from, to time.Time) float64 {
		if to.After(now) {
			to = now
		}
		return max(to.Sub(from).Seconds(), 0)
	}
	rangeSeconds := elapsed(s.start, s.end)
	value := func(sum float64, i int) float64 {
		if starts {
			return sum
		}
		if seconds := elapsed(buckets[i], buckets[i].Add(s.width)); seconds > 0 {
			return sum / seconds
		}
		return 0
	}
	total := func(values []float64) float64 {
		var t float64
		for _, v := range values {
			t += v
		}
		if !starts {
			if rangeSeconds == 0 {
				return 0
			}
			return t / rangeSeconds
		}
		return t
	}
	keys := make([]key, 0, len(series))
	for k := range series {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		ti, tj := total(sums[keys[i]]), total(sums[keys[j]])
		if ti != tj {
			return ti > tj
		}
		return keys[i].app.String() < keys[j].app.String()
	})
	out := apitypes.AccountActivity{
		Measure: measure, Unit: unit, WindowSeconds: int(s.width / time.Second), Start: s.start, End: s.end,
		Series: []apitypes.AccountActivitySeries{},
	}
	other := make([]float64, len(buckets))
	folded := false
	for n, k := range keys {
		if n >= limit {
			folded = true
			for i, v := range sums[k] {
				other[i] += v
			}
			continue
		}
		out.Series = append(out.Series, seriesOut(*series[k], sums[k], buckets, value, total))
	}
	if folded {
		out.Series = append(out.Series, seriesOut(apitypes.AccountActivitySeries{Kind: apitypes.ActivitySeriesKindOther}, other, buckets, value, total))
	}
	all := make([]float64, len(buckets))
	for _, k := range keys {
		for i, v := range sums[k] {
			all[i] += v
		}
	}
	out.Total = total(all)
	return out
}

func seriesOut(sr apitypes.AccountActivitySeries, sums []float64, buckets []time.Time,
	value func(float64, int) float64, total func([]float64) float64,
) apitypes.AccountActivitySeries {
	sr.Total = total(sums)
	sr.Buckets = make([]apitypes.ActivityPoint, len(buckets))
	for i, b := range buckets {
		sr.Buckets[i] = apitypes.ActivityPoint{Timestamp: b, Value: value(sums[i], i)}
	}
	return sr
}
