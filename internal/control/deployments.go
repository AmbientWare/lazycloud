package control

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"slices"
	"sort"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// WorkloadID identifies a workload.
type WorkloadID uuid.UUID

// WorkloadRef addresses a live workload by app, kind and name.
type WorkloadRef struct {
	App  string
	Kind apitypes.WorkloadKind
	Name string
}

// WorkloadState is a workload's desired state.
type WorkloadState string

const (
	WorkloadActive  WorkloadState = "active"
	WorkloadStopped WorkloadState = "stopped"
	WorkloadDeleted WorkloadState = "deleted"
)

// ErrVersionNotFound means the workload has no deployed version with that
// number.
var ErrVersionNotFound = errors.New("version not found")

// WorkloadFilter narrows ListWorkloads.
type WorkloadFilter struct {
	App  *string
	Kind *apitypes.WorkloadKind
	Name *string
	ID   *uuid.UUID
	// Search matches part of the app or workload name.
	Search *string
}

// WorkloadPage is one page of deployed workloads by app, name and kind.
type WorkloadPage struct {
	Workloads []apitypes.Workload
	Next      string
}

// ListWorkloads returns deployed workloads of live apps by app, name and
// kind.
func (c *Control) ListWorkloads(ctx context.Context, workspace identity.WorkspaceID, filter WorkloadFilter, limit int, cursor string) (WorkloadPage, error) {
	after, err := parseWorkloadCursor(cursor)
	if err != nil {
		return WorkloadPage{}, err
	}
	size := pageSize(limit)
	rows, err := c.queries.ListWorkloads(ctx, ListWorkloadsParams{
		WorkspaceID: uuid.UUID(workspace), App: filter.App, Kind: (*string)(filter.Kind), Name: filter.Name, ID: filter.ID,
		Search: lowered(filter.Search), AfterApp: after[0], AfterName: after[1], AfterKind: after[2], MaxRows: size + 1,
	})
	if err != nil {
		return WorkloadPage{}, fmt.Errorf("list workloads: %w", err)
	}
	var page WorkloadPage
	if len(rows) > int(size) {
		rows = rows[:size]
		last := rows[len(rows)-1]
		page.Next = last.AppName + "/" + last.Name + "/" + last.Kind
	}
	page.Workloads = make([]apitypes.Workload, len(rows))
	for n, row := range rows {
		release := row.ReleaseID
		deployed := row.DeployedAt
		page.Workloads[n] = workloadOut(WorkloadViewRow{
			ID: row.ID, AppName: row.AppName, Name: row.Name, Kind: row.Kind, DesiredState: row.DesiredState,
			AppState: row.AppState, Version: row.Version, ReleaseID: &release, CreatedAt: row.CreatedAt, DeployedAt: &deployed,
			RunningContainers: row.RunningContainers,
		})
	}
	return page, nil
}

// The cursor is "<app>/<name>/<kind>" of the last row; no part contains "/".
func parseWorkloadCursor(cursor string) ([3]string, error) {
	var after [3]string
	if cursor == "" {
		return after, nil
	}
	parts := strings.Split(cursor, "/")
	if len(parts) != len(after) || slices.Contains(parts, "") {
		return after, ErrInvalidCursor
	}
	copy(after[:], parts)
	return after, nil
}

// FindWorkload resolves a live workload of a live app.
func (c *Control) FindWorkload(ctx context.Context, workspace identity.WorkspaceID, ref WorkloadRef) (WorkloadID, error) {
	id, err := c.queries.FindWorkload(ctx, FindWorkloadParams{
		WorkspaceID: uuid.UUID(workspace), AppName: ref.App, Kind: string(ref.Kind), Name: ref.Name,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return WorkloadID{}, ErrNotFound
	}
	if err != nil {
		return WorkloadID{}, fmt.Errorf("find workload: %w", err)
	}
	return WorkloadID(id), nil
}

// GetWorkload reads a workload, deleted ones included.
func (c *Control) GetWorkload(ctx context.Context, workspace identity.WorkspaceID, id WorkloadID) (apitypes.Workload, error) {
	return c.workloadView(ctx, c.queries, workspace, id)
}

// Release reads the release the workload's active version runs, or the one
// of version; ErrVersionNotFound when there is none.
func (c *Control) Release(ctx context.Context, workspace identity.WorkspaceID, id WorkloadID, version *int) (apitypes.Release, error) {
	params := WorkloadReleaseParams{WorkspaceID: uuid.UUID(workspace), ID: uuid.UUID(id)}
	if version != nil {
		if *version < 1 || *version > math.MaxInt32 {
			return apitypes.Release{}, ErrVersionNotFound
		}
		params.Version = ptr(int32(*version))
	}
	row, err := c.queries.WorkloadRelease(ctx, params)
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.Release{}, ErrVersionNotFound
	}
	if err != nil {
		return apitypes.Release{}, fmt.Errorf("read release: %w", err)
	}
	var spec apitypes.WorkloadSpec
	if err := json.Unmarshal(row.Spec, &spec); err != nil {
		return apitypes.Release{}, fmt.Errorf("decode release spec: %w", err)
	}
	return apitypes.Release{Id: row.ID, Name: row.Name, Version: versionOf(row.Version), CreatedAt: row.CreatedAt, Spec: spec}, nil
}

// StopWorkload stops admission to the workload. Planning drains its
// deployed releases and cancels their queued tasks; running tasks finish.
func (c *Control) StopWorkload(ctx context.Context, workspace identity.WorkspaceID, id WorkloadID) (apitypes.Workload, error) {
	return c.changeWorkload(ctx, workspace, id, func(ctx context.Context, q *Queries, row LockWorkloadRow) error {
		return applyWorkloadState(ctx, q, row, WorkloadStopped)
	})
}

// StartWorkload lets the workload admit tasks again. With a version, that
// deployed version becomes active first, which rolls back or forward.
func (c *Control) StartWorkload(ctx context.Context, workspace identity.WorkspaceID, id WorkloadID, version *int) (apitypes.Workload, error) {
	return c.changeWorkload(ctx, workspace, id, func(ctx context.Context, q *Queries, row LockWorkloadRow) error {
		if version != nil {
			if *version < 1 || *version > math.MaxInt32 {
				return ErrVersionNotFound
			}
			release, err := q.ReleaseByVersion(ctx, ReleaseByVersionParams{WorkloadID: row.ID, Version: ptr(int32(*version))})
			if errors.Is(err, pgx.ErrNoRows) {
				return ErrVersionNotFound
			}
			if err != nil {
				return fmt.Errorf("find version %d: %w", *version, err)
			}
			if row.ActiveReleaseID == nil || *row.ActiveReleaseID != release {
				if err := q.SetActiveRelease(ctx, SetActiveReleaseParams{ID: row.ID, ReleaseID: release}); err != nil {
					return fmt.Errorf("activate version %d: %w", *version, err)
				}
			}
		} else if row.ActiveReleaseID == nil {
			return ErrNotFound
		}
		return applyWorkloadState(ctx, q, row, WorkloadActive)
	})
}

// DeleteWorkload deletes the workload and every version of it. Planning
// retires its releases: containers stop and queued and running tasks are
// cancelled. The name is free for a later deploy.
func (c *Control) DeleteWorkload(ctx context.Context, workspace identity.WorkspaceID, id WorkloadID) (apitypes.Workload, error) {
	return c.changeWorkload(ctx, workspace, id, func(ctx context.Context, q *Queries, row LockWorkloadRow) error {
		return applyWorkloadState(ctx, q, row, WorkloadDeleted)
	})
}

// changeWorkload applies change to a live workload under its row lock and
// wakes planning in the same transaction.
func (c *Control) changeWorkload(ctx context.Context, workspace identity.WorkspaceID, id WorkloadID,
	change func(context.Context, *Queries, LockWorkloadRow) error,
) (apitypes.Workload, error) {
	var out apitypes.Workload
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		row, err := q.LockWorkload(ctx, LockWorkloadParams{WorkspaceID: uuid.UUID(workspace), ID: uuid.UUID(id)})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock workload: %w", err)
		}
		if WorkloadState(row.DesiredState) == WorkloadDeleted || AppState(row.AppState) == AppDeleted {
			return ErrNotFound
		}
		if err := change(ctx, q, row); err != nil {
			return err
		}
		if err := database.Notify(ctx, tx, database.ChannelExecution, row.ID.String()); err != nil {
			return err
		}
		out, err = c.workloadView(ctx, q, workspace, id)
		return err
	})
	if err != nil {
		return apitypes.Workload{}, fmt.Errorf("change workload %s: %w", uuid.UUID(id), err)
	}
	return out, nil
}

func applyWorkloadState(ctx context.Context, q *Queries, row LockWorkloadRow, state WorkloadState) error {
	if WorkloadState(row.DesiredState) == state {
		return nil
	}
	if err := q.SetWorkloadState(ctx, SetWorkloadStateParams{ID: row.ID, DesiredState: string(state)}); err != nil {
		return fmt.Errorf("set workload state: %w", err)
	}
	return nil
}

func (c *Control) workloadView(ctx context.Context, q *Queries, workspace identity.WorkspaceID, id WorkloadID) (apitypes.Workload, error) {
	row, err := q.WorkloadView(ctx, WorkloadViewParams{WorkspaceID: uuid.UUID(workspace), ID: uuid.UUID(id)})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.Workload{}, ErrNotFound
	}
	if err != nil {
		return apitypes.Workload{}, fmt.Errorf("read workload: %w", err)
	}
	return workloadOut(row), nil
}

func workloadOut(row WorkloadViewRow) apitypes.Workload {
	appState := apitypes.AppState(row.AppState)
	return apitypes.Workload{
		Id: row.ID, App: row.AppName, Name: row.Name, Kind: apitypes.WorkloadKind(row.Kind),
		State: apitypes.WorkloadState(row.DesiredState), AppState: &appState,
		Version: versionOf(row.Version), ReleaseId: row.ReleaseID,
		RunningContainers: int(row.RunningContainers), CreatedAt: row.CreatedAt, DeployedAt: row.DeployedAt,
	}
}

// VersionPage is one page of a workload's deployed versions, newest first.
type VersionPage struct {
	Versions []apitypes.Version
	Next     string
}

// ListVersions returns the workload's deployed versions, newest first.
func (c *Control) ListVersions(ctx context.Context, workspace identity.WorkspaceID, id WorkloadID, limit int, cursor string) (VersionPage, error) {
	if _, err := c.workloadView(ctx, c.queries, workspace, id); err != nil {
		return VersionPage{}, err
	}
	before := int32(math.MaxInt32)
	if cursor != "" {
		n, err := strconv.ParseInt(cursor, 10, 32)
		if err != nil || n < 1 {
			return VersionPage{}, ErrInvalidCursor
		}
		before = int32(n)
	}
	size := pageSize(limit)
	rows, err := c.queries.ListVersions(ctx, ListVersionsParams{
		WorkloadID: uuid.UUID(id), Before: &before, MaxRows: size + 1,
	})
	if err != nil {
		return VersionPage{}, fmt.Errorf("list versions: %w", err)
	}
	var page VersionPage
	if len(rows) > int(size) {
		rows = rows[:size]
		page.Next = strconv.Itoa(int(rows[len(rows)-1].Version))
	}
	page.Versions = make([]apitypes.Version, len(rows))
	for n, row := range rows {
		page.Versions[n] = apitypes.Version{
			ReleaseId: row.ID, Version: int(row.Version), Active: row.Active, CreatedAt: row.CreatedAt,
		}
	}
	return page, nil
}

// PlanDeployment previews a deploy of workloads to app: each listed workload
// is added or redeployed, and each deployed workload the request omits is
// retained, or removed with prune.
func (c *Control) PlanDeployment(ctx context.Context, workspace identity.WorkspaceID, app string, req apitypes.DeploymentPlanRequest) (apitypes.DeploymentPlan, error) {
	type key struct{ kind, name string }
	listed := map[key]bool{}
	for _, w := range req.Workloads {
		k := key{string(w.Kind), w.Name}
		if listed[k] {
			return apitypes.DeploymentPlan{}, &InvalidSpecError{Function: w.Name, Reason: "listed more than once"}
		}
		listed[k] = true
	}
	prune := req.Prune != nil && *req.Prune
	current := map[key]int{}
	var deployed []PlanWorkloadsRow
	appID, err := c.queries.AppByName(ctx, AppByNameParams{WorkspaceID: uuid.UUID(workspace), Name: app})
	switch {
	case errors.Is(err, pgx.ErrNoRows):
	case err != nil:
		return apitypes.DeploymentPlan{}, fmt.Errorf("find app: %w", err)
	default:
		deployed, err = c.queries.PlanWorkloads(ctx, appID)
		if err != nil {
			return apitypes.DeploymentPlan{}, fmt.Errorf("read deployed workloads: %w", err)
		}
		for _, row := range deployed {
			current[key{row.Kind, row.Name}] = int(row.Versions)
		}
	}
	items := make([]apitypes.DeploymentPlanItem, 0, len(req.Workloads)+len(deployed))
	for _, w := range req.Workloads {
		versions, ok := current[key{string(w.Kind), w.Name}]
		action := apitypes.Add
		if ok {
			action = apitypes.Redeploy
		}
		items = append(items, apitypes.DeploymentPlanItem{Kind: w.Kind, Name: w.Name, Action: action, Versions: versions})
	}
	sort.Slice(items, func(a, b int) bool {
		if items[a].Kind != items[b].Kind {
			return items[a].Kind < items[b].Kind
		}
		return items[a].Name < items[b].Name
	})
	for _, row := range deployed {
		if listed[key{row.Kind, row.Name}] {
			continue
		}
		action := apitypes.Retain
		if prune {
			action = apitypes.Remove
		}
		items = append(items, apitypes.DeploymentPlanItem{
			Kind: apitypes.WorkloadKind(row.Kind), Name: row.Name, Action: action, Versions: int(row.Versions),
		})
	}
	return apitypes.DeploymentPlan{App: app, Prune: prune, Items: items}, nil
}

func ptr[T any](v T) *T { return &v }
