package observability

import (
	"context"
	"fmt"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

// PlatformMetrics counts every workspace's accounts and activity now and
// over each of the last buckets of width, the one in progress included.
func (o *Observability) PlatformMetrics(ctx context.Context, width time.Duration, buckets int) (apitypes.AdminMetrics, error) {
	now := time.Now()
	s, err := alignedSpan(nil, nil, width, buckets, now)
	if err != nil {
		return apitypes.AdminMetrics{}, err
	}
	totals, err := o.queries.PlatformTotals(ctx, PlatformTotalsParams{StartAt: s.start, EndAt: s.end})
	if err != nil {
		return apitypes.AdminMetrics{}, fmt.Errorf("count platform totals: %w", err)
	}
	rows, err := o.queries.PlatformBuckets(ctx, PlatformBucketsParams{
		BucketWidth: s.interval(), StartAt: s.start, EndAt: s.end, FromID: s.fromID(), ToID: s.toID(),
	})
	if err != nil {
		return apitypes.AdminMetrics{}, fmt.Errorf("read platform buckets: %w", err)
	}
	out := apitypes.AdminMetrics{
		ObservedAt: now, Users: int(totals.Users), Workspaces: int(totals.Workspaces), Apps: int(totals.Apps),
		LiveContainers: int(totals.LiveContainers), SignedIn: int(totals.SignedIn),
		WindowSeconds: int(width / time.Second), Buckets: make([]apitypes.AdminMetricsBucket, len(rows)),
	}
	for i, r := range rows {
		out.Buckets[i] = apitypes.AdminMetricsBucket{
			Timestamp: r.Bucket, SignUps: int(r.SignUps), SignIns: int(r.SignIns), ActiveWorkspaces: int(r.ActiveWorkspaces),
			Deployments: int(r.Deployments), Tasks: int(r.Tasks), FailedTasks: int(r.FailedTasks),
			Requests: int(r.Requests), FailedRequests: int(r.FailedRequests),
			ContainerStarts: int(r.ContainerStarts), FailedStarts: int(r.FailedStarts),
			StartP50Ms: r.P50Ms, StartP95Ms: r.P95Ms, QueueP50Ms: r.QueueP50Ms, QueueP95Ms: r.QueueP95Ms,
		}
	}
	return out, nil
}
