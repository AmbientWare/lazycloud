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
	appDeleted       = "deleted"
)

// CostQuery selects the account's usage cost over [Start, End).
type CostQuery struct {
	Start, End time.Time
	GroupBy    apitypes.UsageCostGroup
	Workspace  *uuid.UUID
	App        *uuid.UUID
	Category   *apitypes.UsageCostCategory
	Cursor     string
	Limit      int
}

type costCursor struct {
	Cost      int64     `json:"c"`
	Workspace uuid.UUID `json:"w"`
	App       uuid.UUID `json:"a"`
	Workload  uuid.UUID `json:"l"`
	Category  string    `json:"g"`
}

func checkWindow(start, end time.Time) error {
	if !end.After(start) {
		return &InvalidError{Message: "a cost window must end after it starts"}
	}
	if end.Sub(start) > maxCostWindow {
		return &InvalidError{Message: "a cost window may span at most 400 days"}
	}
	return nil
}

// Costs is one page of what the account's usage cost, grouped by app or by
// workload, most expensive first, with the whole window's total. The
// account pays for every workspace it owns, so rows carry their workspace.
// No container runs for one task, so grouping by task groups by workload.
func (b *Billing) Costs(ctx context.Context, user uuid.UUID, q CostQuery) (apitypes.UsageCostPage, error) {
	if err := checkWindow(q.Start, q.End); err != nil {
		return apitypes.UsageCostPage{}, err
	}
	params := CostRowsParams{
		ByWorkload: q.GroupBy != apitypes.UsageCostGroupApp, UserID: user, StartAt: q.Start, EndAt: q.End,
		WorkspaceID: q.Workspace, AppID: q.App, RowLimit: int32(q.Limit + 1), //nolint:gosec // The API bounds the limit to 200.
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
			return apitypes.UsageCostPage{}, &InvalidError{Message: "the cursor is not from a previous page"}
		}
		params.HasCursor, params.AfterCost, params.AfterWorkspace = true, c.Cost, c.Workspace
		params.AfterApp, params.AfterWorkload, params.AfterCategory = c.App, c.Workload, c.Category
	}
	rows, err := b.queries.CostRows(ctx, params)
	if err != nil {
		return apitypes.UsageCostPage{}, fmt.Errorf("read costs: %w", err)
	}
	total, err := b.queries.WindowCost(ctx, WindowCostParams{
		UserID: user, StartAt: q.Start, EndAt: q.End, WorkspaceID: q.Workspace, AppID: q.App, Category: params.Category,
	})
	if err != nil {
		return apitypes.UsageCostPage{}, fmt.Errorf("read window cost: %w", err)
	}
	page := apitypes.UsageCostPage{
		Start: q.Start, End: q.End, Currency: Currency, GroupBy: q.GroupBy, CostNanos: total, Rows: []apitypes.UsageCostRow{},
	}
	if len(rows) > q.Limit {
		last := rows[q.Limit-1]
		raw, err := json.Marshal(costCursor{Cost: last.CostNanos, Workspace: last.WorkspaceID, App: last.AppKey, Workload: last.WorkloadKey, Category: last.Category})
		if err != nil {
			return apitypes.UsageCostPage{}, fmt.Errorf("encode cursor: %w", err)
		}
		next := base64.RawURLEncoding.EncodeToString(raw)
		page.NextCursor = &next
		rows = rows[:q.Limit]
	}
	names, err := b.costNames(ctx, rows)
	if err != nil {
		return apitypes.UsageCostPage{}, err
	}
	for _, r := range rows {
		page.Rows = append(page.Rows, names.row(r))
	}
	return page, nil
}

type costNames struct {
	workspaces map[uuid.UUID]string
	apps       map[uuid.UUID]string
	workloads  map[uuid.UUID][2]string
}

// costNames resolves the names a page shows. A deleted app or workspace
// keeps its cost and loses its name.
func (b *Billing) costNames(ctx context.Context, rows []CostRowsRow) (costNames, error) {
	var workspaces, apps, workloads []uuid.UUID
	for _, r := range rows {
		workspaces = append(workspaces, r.WorkspaceID)
		if r.AppKey != uuid.Nil {
			apps = append(apps, r.AppKey)
		}
		if r.WorkloadKey != uuid.Nil {
			workloads = append(workloads, r.WorkloadKey)
		}
	}
	out := costNames{workspaces: map[uuid.UUID]string{}, apps: map[uuid.UUID]string{}, workloads: map[uuid.UUID][2]string{}}
	ws, err := b.queries.WorkspaceNames(ctx, workspaces)
	if err != nil {
		return costNames{}, fmt.Errorf("read workspace names: %w", err)
	}
	for _, w := range ws {
		out.workspaces[w.ID] = w.Name
	}
	as, err := b.queries.AppNames(ctx, apps)
	if err != nil {
		return costNames{}, fmt.Errorf("read app names: %w", err)
	}
	for _, a := range as {
		if a.State != appDeleted {
			out.apps[a.ID] = a.Name
		}
	}
	wls, err := b.queries.WorkloadNames(ctx, workloads)
	if err != nil {
		return costNames{}, fmt.Errorf("read workload names: %w", err)
	}
	for _, w := range wls {
		out.workloads[w.ID] = [2]string{w.Name, w.Kind}
	}
	return out, nil
}

func (n costNames) row(r CostRowsRow) apitypes.UsageCostRow {
	out := apitypes.UsageCostRow{WorkspaceId: r.WorkspaceID, CostNanos: r.CostNanos}
	if name, ok := n.workspaces[r.WorkspaceID]; ok {
		out.WorkspaceName = &name
	}
	if r.AppKey != uuid.Nil {
		id := r.AppKey
		out.AppId = &id
		if name, ok := n.apps[id]; ok {
			out.AppName = &name
		}
	}
	if r.WorkloadKey != uuid.Nil {
		id := r.WorkloadKey
		out.WorkloadId = &id
		if names, ok := n.workloads[id]; ok && out.AppName != nil {
			out.WorkloadName, out.WorkloadKind = &names[0], &names[1]
		}
	}
	switch r.Category {
	case categoryImageBuild:
		c := apitypes.UsageCostCategoryImageBuild
		out.Category = &c
	case "":
		if r.AppKey == uuid.Nil {
			c := apitypes.UsageCostCategoryUnattributed
			out.Category = &c
		}
	}
	compute := apitypes.BilledDimensionComputeRuntime
	out.Components = []apitypes.UsageCostComponent{
		{Dimension: compute, Component: apitypes.ContainerTime, Quantity: float32(r.Seconds), CostNanos: r.ContainerNanos},
		{Dimension: compute, Component: apitypes.Cpu, Quantity: float32(r.CoreSeconds), CostNanos: r.CpuNanos},
		{Dimension: compute, Component: apitypes.Memory, Quantity: float32(r.GibSeconds), CostNanos: r.MemoryNanos},
	}
	if r.CardSeconds > 0 {
		out.Components = append(out.Components, apitypes.UsageCostComponent{
			Dimension: compute, Component: apitypes.Gpu, Quantity: float32(r.CardSeconds), CostNanos: r.GpuNanos,
		})
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
		return apitypes.UsageCostSeries{}, &InvalidError{Message: fmt.Sprintf(
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
	costs := make(map[int32]int64, len(rows))
	for _, r := range rows {
		costs[r.Bucket] = r.CostNanos
	}
	out := apitypes.UsageCostSeries{
		Start: start, End: end, Currency: Currency, Bucket: bucket, SubscriptionCreditNanos: covered,
		Intervals: make([]apitypes.UsageCostInterval, count),
	}
	for n := range count {
		from := start.Add(time.Duration(n) * width)
		interval := apitypes.UsageCostInterval{StartedAt: from, EndedAt: minTime(from.Add(width), end), Dimensions: []apitypes.UsageCostDimension{}}
		if cost, ok := costs[int32(n)]; ok { //nolint:gosec // Bounded by maxCostIntervals.
			interval.CostNanos = cost
			interval.Dimensions = append(interval.Dimensions, apitypes.UsageCostDimension{Dimension: apitypes.BilledDimensionComputeRuntime, CostNanos: cost})
		}
		out.CostNanos += interval.CostNanos
		out.Intervals[n] = interval
	}
	return out, nil
}
