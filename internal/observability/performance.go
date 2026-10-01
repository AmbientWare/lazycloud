package observability

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// RangeQuery is an optional range and bucket width.
type RangeQuery struct {
	Start, End *time.Time
	// Width is the bucket width; zero means an hour.
	Width time.Duration
}

func (q RangeQuery) width() time.Duration {
	if q.Width <= 0 {
		return time.Hour
	}
	return q.Width
}

// DeploymentPerformance buckets a deployment's tasks by submission time:
// run time percentiles of finished tasks, outcomes and the containers
// started for it. Buckets without either are left out. The default range is
// the last 24 hours.
func (o *Observability) DeploymentPerformance(ctx context.Context, ws identity.WorkspaceID, deployment uuid.UUID, q RangeQuery) (apitypes.DeploymentPerformance, error) {
	if _, err := o.queries.WorkloadInWorkspace(ctx, WorkloadInWorkspaceParams{ID: deployment, WorkspaceID: uuid.UUID(ws)}); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return apitypes.DeploymentPerformance{}, ErrNotFound
		}
		return apitypes.DeploymentPerformance{}, fmt.Errorf("read deployment: %w", err)
	}
	width := q.width()
	start := q.Start
	if start == nil {
		end := time.Now()
		if q.End != nil {
			end = *q.End
		}
		day := end.Add(-24 * time.Hour)
		start = &day
	}
	s, err := alignedSpan(start, q.End, width, 0, time.Now())
	if err != nil {
		return apitypes.DeploymentPerformance{}, err
	}
	rows, err := o.queries.DeploymentPerformance(ctx, DeploymentPerformanceParams{
		BucketWidth: s.interval(), WorkloadID: deployment, FromID: s.fromID(), ToID: s.toID(), StartAt: s.start, EndAt: s.end,
	})
	if err != nil {
		return apitypes.DeploymentPerformance{}, fmt.Errorf("read deployment performance: %w", err)
	}
	out := apitypes.DeploymentPerformance{
		DeploymentId: deployment, WindowSeconds: int(width / time.Second), Start: s.start, End: s.end,
		Buckets: make([]apitypes.PerformanceBucket, 0, len(rows)),
	}
	for _, r := range rows {
		out.Buckets = append(out.Buckets, apitypes.PerformanceBucket{
			Timestamp: r.Bucket, Count: int(r.Finished), P50Ms: r.P50Ms, P95Ms: r.P95Ms, ColdStarts: int(r.ColdStarts),
			StatusCounts: apitypes.TaskStatusCounts{
				Queued: int(r.Queued), Running: int(r.Running), Succeeded: int(r.Succeeded),
				Failed: int(r.Failed), Cancelled: int(r.Cancelled),
			},
		})
	}
	return out, nil
}

// TaskFilter narrows workspace task metrics to an app or one function.
type TaskFilter struct {
	App, Function *string
}

// TaskMetrics summarizes the tasks submitted in the range: outcomes,
// failure rate, and run time and startup percentiles of those that ran. The
// default range is the last 24 hours.
func (o *Observability) TaskMetrics(ctx context.Context, ws identity.WorkspaceID, start, end *time.Time, f TaskFilter) (apitypes.TaskMetrics, error) {
	s, err := exactSpan(start, end, 24*time.Hour, time.Now())
	if err != nil {
		return apitypes.TaskMetrics{}, err
	}
	r, err := o.queries.TaskMetrics(ctx, TaskMetricsParams{
		WorkspaceID: uuid.UUID(ws), FromID: s.fromID(), ToID: s.toID(), StartAt: s.start, EndAt: s.end,
		App: f.App, Function: f.Function,
	})
	if err != nil {
		return apitypes.TaskMetrics{}, fmt.Errorf("read task metrics: %w", err)
	}
	out := apitypes.TaskMetrics{
		Start: s.start, End: s.end, Total: int(r.Total),
		StatusCounts: apitypes.TaskStatusCounts{
			Queued: int(r.Queued), Running: int(r.Running), Succeeded: int(r.Succeeded),
			Failed: int(r.Failed), Cancelled: int(r.Cancelled),
		},
	}
	if r.Total > 0 {
		out.FailureRate = float64(r.Failed) / float64(r.Total)
	}
	if r.Finished > 0 {
		out.AverageRuntimeMs = &r.AverageRuntimeMs
		out.RuntimeMsP50, out.RuntimeMsP95, out.RuntimeMsP99 = &r.RuntimeP50, &r.RuntimeP95, &r.RuntimeP99
	}
	if r.Started > 0 {
		out.StartupMsP50, out.StartupMsP95 = &r.StartupP50, &r.StartupP95
	}
	return out, nil
}

// TaskActivity counts tasks submitted per bucket and status: one series per
// app, or per function of app when it is set. Buckets are dense; the default
// range is the last 24 buckets.
func (o *Observability) TaskActivity(ctx context.Context, ws identity.WorkspaceID, q RangeQuery, app *string) (apitypes.TaskActivity, error) {
	width := q.width()
	s, err := alignedSpan(q.Start, q.End, width, 24, time.Now())
	if err != nil {
		return apitypes.TaskActivity{}, err
	}
	rows, err := o.queries.TaskActivity(ctx, TaskActivityParams{
		App: app, BucketWidth: s.interval(), WorkspaceID: uuid.UUID(ws), FromID: s.fromID(), ToID: s.toID(),
		StartAt: s.start, EndAt: s.end,
	})
	if err != nil {
		return apitypes.TaskActivity{}, fmt.Errorf("read task activity: %w", err)
	}
	type key struct {
		app      uuid.UUID
		function string
	}
	starts := s.buckets()
	index := make(map[time.Time]int, len(starts))
	for i, b := range starts {
		index[b] = i
	}
	series := map[key]*apitypes.ActivitySeries{}
	var order []key
	for _, r := range rows {
		k := key{app: r.AppID, function: r.FunctionName}
		sr, ok := series[k]
		if !ok {
			appID := r.AppID
			sr = &apitypes.ActivitySeries{App: r.AppName, AppId: &appID, Buckets: make([]apitypes.ActivityBucket, len(starts))}
			if app != nil {
				fn := r.FunctionName
				sr.Function = &fn
			}
			for i, b := range starts {
				sr.Buckets[i].Timestamp = b
			}
			series[k] = sr
			order = append(order, k)
		}
		i, ok := index[r.Bucket.UTC()]
		if !ok {
			continue
		}
		c := &sr.Buckets[i].StatusCounts
		c.Queued += int(r.Queued)
		c.Running += int(r.Running)
		c.Succeeded += int(r.Succeeded)
		c.Failed += int(r.Failed)
		c.Cancelled += int(r.Cancelled)
		sr.Total += int(r.Queued + r.Running + r.Succeeded + r.Failed + r.Cancelled)
	}
	out := apitypes.TaskActivity{
		WindowSeconds: int(width / time.Second), Start: s.start, End: s.end,
		Series: make([]apitypes.ActivitySeries, 0, len(order)),
	}
	for _, k := range order {
		out.Series = append(out.Series, *series[k])
	}
	sort.SliceStable(out.Series, func(i, j int) bool { return out.Series[i].Total > out.Series[j].Total })
	return out, nil
}
