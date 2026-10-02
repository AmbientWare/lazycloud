package execution

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"

	"github.com/google/uuid"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// SandboxPage is one page of sandbox containers, newest first.
type SandboxPage struct {
	Sandboxes []apitypes.Sandbox
	Next      string
}

// sandboxStatus is what a sandbox container shows: stopped on its own or
// by request, or failed otherwise.
func sandboxStatus(state ContainerState, reason *string) apitypes.SandboxStatus {
	switch state {
	case ContainerPending, ContainerStarting:
		return apitypes.SandboxStatusPending
	case ContainerReady:
		return apitypes.SandboxStatusRunning
	case ContainerDraining:
		return apitypes.SandboxStatusStopping
	case ContainerStopped:
		if reason != nil && (*reason == string(StopRequested) || *reason == string(StopExited)) {
			return apitypes.SandboxStatusStopped
		}
		return apitypes.SandboxStatusFailed
	}
	return apitypes.SandboxStatusFailed
}

// SandboxFilter narrows a sandbox listing to an app and to sandboxes whose
// name, app name or container id contains Search, ignoring case.
type SandboxFilter struct {
	App    *string
	Search *string
}

// ListSandboxes returns the workspace's sandbox containers that match
// filter, newest first.
func (e *Execution) ListSandboxes(ctx context.Context, workspace identity.WorkspaceID, filter SandboxFilter, limit int, cursor string) (SandboxPage, error) {
	before := uuid.Max
	if cursor != "" {
		id, err := uuid.Parse(cursor)
		if err != nil {
			return SandboxPage{}, ErrInvalidCursor
		}
		before = id
	}
	size := pageSize(limit)
	var search *string
	if filter.Search != nil {
		lower := strings.ToLower(*filter.Search)
		search = &lower
	}
	rows, err := e.queries.ListSandboxes(ctx, ListSandboxesParams{
		WorkspaceID: uuid.UUID(workspace), Before: before, App: filter.App, Search: search, MaxRows: size + 1,
	})
	if err != nil {
		return SandboxPage{}, fmt.Errorf("list sandboxes: %w", err)
	}
	var page SandboxPage
	if len(rows) > int(size) {
		rows = rows[:size]
		page.Next = rows[len(rows)-1].ID.String()
	}
	page.Sandboxes = make([]apitypes.Sandbox, len(rows))
	for n, row := range rows {
		var spec apitypes.WorkloadSpec
		if err := json.Unmarshal(row.Spec, &spec); err != nil {
			return SandboxPage{}, fmt.Errorf("decode release spec: %w", err)
		}
		gpu := []apitypes.GpuType{}
		if spec.Resources.Gpu != nil {
			gpu = *spec.Resources.Gpu
		}
		s := apitypes.Sandbox{
			Id: row.ID, ReleaseId: row.ReleaseID, App: row.AppName, Name: row.WorkloadName,
			Status: sandboxStatus(ContainerState(row.State), row.StopReason), Gpu: gpu,
			CreatedAt: row.CreatedAt, ReadyAt: row.ReadyAt, StoppedAt: row.StoppedAt,
		}
		if row.ReadyAt != nil {
			ms := row.ReadyAt.Sub(row.CreatedAt).Milliseconds()
			s.TimeToStartedMs = &ms
			if row.StoppedAt != nil {
				life := row.StoppedAt.Sub(*row.ReadyAt).Milliseconds()
				s.LifetimeMs = &life
			}
		}
		page.Sandboxes[n] = s
	}
	return page, nil
}

// SandboxStats counts the workspace's sandboxes, only app's when it is set.
func (e *Execution) SandboxStats(ctx context.Context, workspace identity.WorkspaceID, app *string) (apitypes.SandboxStats, error) {
	row, err := e.queries.SandboxStats(ctx, SandboxStatsParams{WorkspaceID: uuid.UUID(workspace), App: app})
	if err != nil {
		return apitypes.SandboxStats{}, fmt.Errorf("count sandboxes: %w", err)
	}
	days, err := e.queries.SandboxCreatedDays(ctx, SandboxCreatedDaysParams{WorkspaceID: uuid.UUID(workspace), App: app})
	if err != nil {
		return apitypes.SandboxStats{}, fmt.Errorf("count sandboxes by day: %w", err)
	}
	out := apitypes.SandboxStats{
		Concurrent: int(row.Concurrent), TotalCreated: int(row.TotalCreated),
		RatePerSecond: float32(row.CreatedDay) / 86400,
		StatusCounts: map[string]int{
			string(apitypes.SandboxStatusPending):  int(row.Pending),
			string(apitypes.SandboxStatusRunning):  int(row.Running),
			string(apitypes.SandboxStatusStopping): int(row.Stopping),
			string(apitypes.SandboxStatusStopped):  int(row.Stopped),
			string(apitypes.SandboxStatusFailed):   int(row.Failed),
		},
		CreatedBuckets: make([]apitypes.SandboxCreatedBucket, len(days)),
	}
	for n, d := range days {
		out.CreatedBuckets[n] = apitypes.SandboxCreatedBucket{Timestamp: d.Day, Count: int(d.Created)}
	}
	return out, nil
}
