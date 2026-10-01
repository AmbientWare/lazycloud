package control

import (
	"context"
	"errors"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// WorkloadID identifies a workload, which the API calls a deployment.
type WorkloadID uuid.UUID

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

// DeploymentFilter narrows ListDeployments.
type DeploymentFilter struct {
	App  *string
	Name *string
}

// DeploymentPage is one page of deployed workloads by app and name.
type DeploymentPage struct {
	Deployments []apitypes.DeployedWorkload
	Next        string
}

// ListDeployments returns deployed workloads of live apps by app and name.
func (c *Control) ListDeployments(ctx context.Context, workspace identity.WorkspaceID, filter DeploymentFilter, limit int, cursor string) (DeploymentPage, error) {
	afterApp, afterName, err := parseDeploymentCursor(cursor)
	if err != nil {
		return DeploymentPage{}, err
	}
	size := pageSize(limit)
	rows, err := c.queries.ListDeployments(ctx, ListDeploymentsParams{
		WorkspaceID: uuid.UUID(workspace), App: filter.App, Name: filter.Name,
		AfterApp: afterApp, AfterName: afterName, MaxRows: size + 1,
	})
	if err != nil {
		return DeploymentPage{}, fmt.Errorf("list deployments: %w", err)
	}
	var page DeploymentPage
	if len(rows) > int(size) {
		rows = rows[:size]
		last := rows[len(rows)-1]
		page.Next = last.AppName + "/" + last.Name
	}
	page.Deployments = make([]apitypes.DeployedWorkload, len(rows))
	for n, row := range rows {
		release := row.ReleaseID
		deployed := row.DeployedAt
		page.Deployments[n] = workloadOut(WorkloadViewRow{
			ID: row.ID, AppName: row.AppName, Name: row.Name, Kind: row.Kind, DesiredState: row.DesiredState,
			AppState: row.AppState, Version: row.Version, ReleaseID: &release, CreatedAt: row.CreatedAt, DeployedAt: &deployed,
			RunningContainers: row.RunningContainers,
		})
	}
	return page, nil
}

// The cursor is "<app>/<name>" of the last row; neither name contains "/".
func parseDeploymentCursor(cursor string) (string, string, error) {
	if cursor == "" {
		return "", "", nil
	}
	app, name, ok := strings.Cut(cursor, "/")
	if !ok || app == "" || name == "" {
		return "", "", ErrInvalidCursor
	}
	return app, name, nil
}

// GetDeployment reads a workload, deleted ones included.
func (c *Control) GetDeployment(ctx context.Context, workspace identity.WorkspaceID, id WorkloadID) (apitypes.DeployedWorkload, error) {
	return c.workloadView(ctx, c.queries, workspace, id)
}

// StopDeployment stops admission to the workload. Planning drains its
// deployed releases and cancels their queued tasks; running tasks finish.
func (c *Control) StopDeployment(ctx context.Context, workspace identity.WorkspaceID, id WorkloadID) (apitypes.DeployedWorkload, error) {
	return c.changeWorkload(ctx, workspace, id, func(ctx context.Context, q *Queries, row LockWorkloadRow) error {
		return applyWorkloadState(ctx, q, row, WorkloadStopped)
	})
}

// StartDeployment lets the workload admit tasks again. With a version, that
// deployed version becomes active first, which rolls back or forward.
func (c *Control) StartDeployment(ctx context.Context, workspace identity.WorkspaceID, id WorkloadID, version *int) (apitypes.DeployedWorkload, error) {
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

// DeleteDeployment deletes the workload and every version of it. Planning
// retires its releases: containers stop and queued and running tasks are
// cancelled. The name is free for a later deploy.
func (c *Control) DeleteDeployment(ctx context.Context, workspace identity.WorkspaceID, id WorkloadID) (apitypes.DeployedWorkload, error) {
	return c.changeWorkload(ctx, workspace, id, func(ctx context.Context, q *Queries, row LockWorkloadRow) error {
		return applyWorkloadState(ctx, q, row, WorkloadDeleted)
	})
}

// changeWorkload applies change to a live workload under its row lock and
// wakes planning in the same transaction.
func (c *Control) changeWorkload(ctx context.Context, workspace identity.WorkspaceID, id WorkloadID,
	change func(context.Context, *Queries, LockWorkloadRow) error,
) (apitypes.DeployedWorkload, error) {
	var out apitypes.DeployedWorkload
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
		return apitypes.DeployedWorkload{}, fmt.Errorf("change deployment %s: %w", uuid.UUID(id), err)
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

func (c *Control) workloadView(ctx context.Context, q *Queries, workspace identity.WorkspaceID, id WorkloadID) (apitypes.DeployedWorkload, error) {
	row, err := q.WorkloadView(ctx, WorkloadViewParams{WorkspaceID: uuid.UUID(workspace), ID: uuid.UUID(id)})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.DeployedWorkload{}, ErrNotFound
	}
	if err != nil {
		return apitypes.DeployedWorkload{}, fmt.Errorf("read workload: %w", err)
	}
	return workloadOut(row), nil
}

func workloadOut(row WorkloadViewRow) apitypes.DeployedWorkload {
	appState := apitypes.AppState(row.AppState)
	return apitypes.DeployedWorkload{
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
