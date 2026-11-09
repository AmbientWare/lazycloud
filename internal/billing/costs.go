package billing

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"time"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
)

const (
	// maxCostWindow bounds one cost question: a year of days, scanned over
	// the payer index.
	maxCostWindow    = 400 * 24 * time.Hour
	maxCostIntervals = 400
)

// CostQuery selects the account's usage cost over [Start, End).
type CostQuery struct {
	Start, End time.Time
	GroupBy    apitypes.UsageCostGroup
	Workspace  *uuid.UUID
	App        *uuid.UUID
	Workload   *uuid.UUID
	Category   *apitypes.UsageCostCategory
	Cursor     string
	Limit      int
}

type costCursor struct {
	Cost      int64     `json:"c"`
	Workspace uuid.UUID `json:"w"`
	App       uuid.UUID `json:"a"`
	Workload  uuid.UUID `json:"l"`
	Task      uuid.UUID `json:"t"`
	Category  string    `json:"g"`
	Disk      uuid.UUID `json:"d"`
}

func checkWindow(start, end time.Time) error {
	if !end.After(start) {
		return &apitypes.Error{Code: apitypes.InvalidRequest, Message: "a cost window must end after it starts"}
	}
	if end.Sub(start) > maxCostWindow {
		return &apitypes.Error{Code: apitypes.InvalidRequest, Message: "a cost window may span at most 400 days"}
	}
	return nil
}

// Costs is one page of what the account's usage cost, grouped by app or by
// workload, most expensive first, with the whole window's total. The
// account pays for every workspace it owns, so rows carry their workspace.
// Each disk is its own row; volumes are usage without an app, and an app's
// artifacts are its own row by workload. Grouping by task splits each
// workload's container cost among its runs by the time they ran, leaving
// idle container time on the workload's row.
func (b *Billing) Costs(ctx context.Context, user uuid.UUID, q CostQuery) (apitypes.UsageCostPage, error) {
	if err := checkWindow(q.Start, q.End); err != nil {
		return apitypes.UsageCostPage{}, err
	}
	params := CostRowsParams{
		ByWorkload: q.GroupBy != apitypes.UsageCostGroupApp, ByTask: q.GroupBy == apitypes.UsageCostGroupTask,
		UserID: user, StartAt: q.Start, EndAt: q.End,
		WorkspaceID: q.Workspace, AppID: q.App, WorkloadID: q.Workload, RowLimit: int32(q.Limit + 1), //nolint:gosec // The API bounds the limit to 200.
	}
	if q.Category != nil {
		params.Category = string(*q.Category)
	}
	if q.Cursor != "" {
		raw, err := base64.RawURLEncoding.DecodeString(q.Cursor)
		var c costCursor
		if err == nil {
			err = json.Unmarshal(raw, &c)
		}
		if err != nil {
			return apitypes.UsageCostPage{}, &apitypes.Error{Code: apitypes.InvalidRequest, Message: "the cursor is not from a previous page"}
		}
		params.HasCursor, params.AfterCost, params.AfterWorkspace = true, c.Cost, c.Workspace
		params.AfterApp, params.AfterWorkload, params.AfterCategory, params.AfterDisk = c.App, c.Workload, c.Category, c.Disk
		params.AfterTask = c.Task
	}
	rows, err := b.queries.CostRows(ctx, params)
	if err != nil {
		return apitypes.UsageCostPage{}, fmt.Errorf("read costs: %w", err)
	}
	total, err := b.queries.WindowCost(ctx, WindowCostParams{
		UserID: user, StartAt: q.Start, EndAt: q.End, WorkspaceID: q.Workspace, AppID: q.App, WorkloadID: q.Workload,
		Category: params.Category,
	})
	if err != nil {
		return apitypes.UsageCostPage{}, fmt.Errorf("read window cost: %w", err)
	}
	page := apitypes.UsageCostPage{
		Start: q.Start, End: q.End, Currency: Currency, GroupBy: q.GroupBy, CostNanos: total, Rows: []apitypes.UsageCostRow{},
	}
	if len(rows) > q.Limit {
		last := rows[q.Limit-1]
		raw, err := json.Marshal(costCursor{
			Cost: last.CostNanos, Workspace: last.WorkspaceID, App: last.AppKey, Workload: last.WorkloadKey,
			Task: last.TaskKey, Category: last.Category, Disk: last.DiskKey,
		})
		if err != nil {
			return apitypes.UsageCostPage{}, fmt.Errorf("encode cursor: %w", err)
		}
		next := base64.RawURLEncoding.EncodeToString(raw)
		page.NextCursor = &next
		rows = rows[:q.Limit]
	}
	for _, r := range rows {
		page.Rows = append(page.Rows, costRow(r))
	}
	return page, nil
}

func costRow(r CostRowsRow) apitypes.UsageCostRow {
	out := apitypes.UsageCostRow{WorkspaceId: r.WorkspaceID, CostNanos: r.CostNanos, WorkspaceName: r.WorkspaceName}
	if r.AppKey != uuid.Nil {
		id := r.AppKey
		out.AppId, out.AppName = &id, r.AppName
	}
	if r.WorkloadKey != uuid.Nil {
		id := r.WorkloadKey
		out.WorkloadId = &id
		if out.AppName != nil {
			out.WorkloadName, out.WorkloadKind = r.WorkloadName, r.WorkloadKind
		}
	}
	if r.TaskKey != uuid.Nil {
		id := r.TaskKey
		out.TaskId = &id
	}
	switch r.Category {
	case categoryImageBuild:
		c := apitypes.UsageCostCategoryImageBuild
		out.Category = &c
	case kindDisk:
		c := apitypes.UsageCostCategoryDisk
		out.Category = &c
		id := r.DiskKey
		out.DiskId, out.DiskName = &id, r.DiskName
	case kindArtifacts:
		name, kind := "Artifacts", kindArtifacts
		out.WorkloadName, out.WorkloadKind = &name, &kind
	case "":
		if r.AppKey == uuid.Nil {
			c := apitypes.UsageCostCategoryUnattributed
			out.Category = &c
		}
	}
	compute := apitypes.BilledDimensionComputeRuntime
	if r.Seconds > 0 {
		out.Components = append(out.Components,
			apitypes.UsageCostComponent{Dimension: compute, Component: apitypes.UsageCostComponentKindContainerTime, Quantity: float32(r.Seconds), CostNanos: r.ContainerNanos},
			apitypes.UsageCostComponent{Dimension: compute, Component: apitypes.UsageCostComponentKindCpu, Quantity: float32(r.CoreSeconds), CostNanos: r.CpuNanos},
			apitypes.UsageCostComponent{Dimension: compute, Component: apitypes.UsageCostComponentKindMemory, Quantity: float32(r.GibSeconds), CostNanos: r.MemoryNanos},
		)
	}
	if r.CardSeconds > 0 {
		out.Components = append(out.Components, apitypes.UsageCostComponent{
			Dimension: compute, Component: apitypes.UsageCostComponentKindGpu, Quantity: float32(r.CardSeconds), CostNanos: r.GpuNanos,
		})
	}
	if r.VolumeGibSeconds > 0 || r.VolumeNanos > 0 {
		out.Components = append(out.Components, apitypes.UsageCostComponent{
			Dimension: apitypes.BilledDimensionVolumeStorage, Component: apitypes.UsageCostComponentKindVolumeStorage,
			Quantity: float32(r.VolumeGibSeconds), CostNanos: r.VolumeNanos,
		})
	}
	if r.DiskGibSeconds > 0 || r.DiskNanos > 0 {
		out.Components = append(out.Components, apitypes.UsageCostComponent{
			Dimension: apitypes.BilledDimensionDisk, Component: apitypes.UsageCostComponentKindDisk,
			Quantity: float32(r.DiskGibSeconds), CostNanos: r.DiskNanos,
		})
	}
	if r.EgressGib > 0 || r.EgressNanos > 0 {
		out.Components = append(out.Components, apitypes.UsageCostComponent{
			Dimension: apitypes.BilledDimensionNetworkEgress, Component: apitypes.UsageCostComponentKindEgress,
			Quantity: float32(r.EgressGib), CostNanos: r.EgressNanos,
		})
	}
	if out.Components == nil {
		out.Components = []apitypes.UsageCostComponent{}
	}
	return out
}

// CostSeries is the account's spend over [start, end) in whole buckets from
// start, every bucket present, and how much of it subscription credit
// covered.
func (b *Billing) CostSeries(ctx context.Context, user uuid.UUID, start, end time.Time, bucket apitypes.UsageCostBucket) (apitypes.UsageCostSeries, error) {
	if err := checkWindow(start, end); err != nil {
		return apitypes.UsageCostSeries{}, err
	}
	width := 24 * time.Hour
	if bucket == apitypes.Hour {
		width = time.Hour
	}
	count := int((end.Sub(start) + width - 1) / width)
	if count > maxCostIntervals {
		return apitypes.UsageCostSeries{}, &apitypes.Error{Code: apitypes.InvalidRequest, Message: fmt.Sprintf(
			"a cost series holds at most %d intervals; this window is %d of them", maxCostIntervals, count)}
	}
	rows, err := b.queries.CostBuckets(ctx, CostBucketsParams{UserID: user, StartAt: start, EndAt: end, WidthSeconds: width.Seconds()})
	if err != nil {
		return apitypes.UsageCostSeries{}, fmt.Errorf("read cost buckets: %w", err)
	}
	covered, err := b.queries.SubscriptionCovered(ctx, SubscriptionCoveredParams{UserID: user, StartAt: start, EndAt: end})
	if err != nil {
		return apitypes.UsageCostSeries{}, fmt.Errorf("read subscription coverage: %w", err)
	}
	costs := make(map[int32][]apitypes.UsageCostDimension, len(rows))
	for _, r := range rows {
		costs[r.Bucket] = append(costs[r.Bucket], apitypes.UsageCostDimension{Dimension: apitypes.BilledDimension(r.Dimension), CostNanos: r.CostNanos})
	}
	out := apitypes.UsageCostSeries{
		Start: start, End: end, Currency: Currency, Bucket: bucket, SubscriptionCreditNanos: covered,
		Intervals: make([]apitypes.UsageCostInterval, count),
	}
	for n := range count {
		from := start.Add(time.Duration(n) * width)
		interval := apitypes.UsageCostInterval{StartedAt: from, EndedAt: minTime(from.Add(width), end), Dimensions: []apitypes.UsageCostDimension{}}
		for _, d := range costs[int32(n)] { //nolint:gosec // Bounded by maxCostIntervals.
			interval.CostNanos += d.CostNanos
			interval.Dimensions = append(interval.Dimensions, d)
		}
		out.CostNanos += interval.CostNanos
		out.Intervals[n] = interval
	}
	return out, nil
}
