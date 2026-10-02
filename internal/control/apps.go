package control

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/identity"
)

// ErrInvalidCursor means a page cursor did not come from this listing.
var ErrInvalidCursor = errors.New("invalid page cursor")

// AppState is an app's desired state.
type AppState string

const (
	AppActive  AppState = "active"
	AppPaused  AppState = "paused"
	AppDeleted AppState = "deleted"
)

// AppFilter narrows an app listing.
type AppFilter struct {
	State *AppState
	// Search matches part of the app name.
	Search *string
}

// lowered is a search term in the lowercase the names are compared in.
func lowered(term *string) *string {
	if term == nil {
		return nil
	}
	l := strings.ToLower(*term)
	return &l
}

// AppPage is one page of apps by name.
type AppPage struct {
	Apps []apitypes.App
	// Next is the cursor of the following page, or empty on the last one.
	Next string
}

// ListApps returns live apps with a deployed workload by name, narrowed by
// the filter, starting after the cursor.
func (c *Control) ListApps(ctx context.Context, workspace identity.WorkspaceID, filter AppFilter, limit int, cursor string) (AppPage, error) {
	size := pageSize(limit)
	params := ListAppsParams{WorkspaceID: uuid.UUID(workspace), Search: lowered(filter.Search), After: cursor, MaxRows: size + 1}
	if filter.State != nil {
		s := string(*filter.State)
		params.State = &s
	}
	rows, err := c.queries.ListApps(ctx, params)
	if err != nil {
		return AppPage{}, fmt.Errorf("list apps: %w", err)
	}
	var page AppPage
	if len(rows) > int(size) {
		rows = rows[:size]
		page.Next = rows[len(rows)-1].Name
	}
	page.Apps = make([]apitypes.App, len(rows))
	for n, row := range rows {
		page.Apps[n] = appOut(AppViewRow(row))
	}
	return page, nil
}

// GetApp reads an app by name or id. An id finds a deleted app too.
func (c *Control) GetApp(ctx context.Context, workspace identity.WorkspaceID, ref string) (apitypes.App, error) {
	id, err := c.resolveApp(ctx, c.queries, workspace, ref)
	if err != nil {
		return apitypes.App{}, err
	}
	return c.appView(ctx, c.queries, workspace, id)
}

// PauseApp stops admission to every workload of the app. Execution planning
// drains its containers and cancels its queued tasks; running tasks finish.
func (c *Control) PauseApp(ctx context.Context, workspace identity.WorkspaceID, ref string) (apitypes.App, error) {
	return c.setAppState(ctx, workspace, ref, AppPaused)
}

// ResumeApp lets a paused app admit tasks and keep warm containers again.
func (c *Control) ResumeApp(ctx context.Context, workspace identity.WorkspaceID, ref string) (apitypes.App, error) {
	return c.setAppState(ctx, workspace, ref, AppActive)
}

// DeleteApp deletes the app. Execution planning retires every release of it:
// containers stop and queued and running tasks are cancelled. The rows stay
// for task history and the name is free for a new app.
func (c *Control) DeleteApp(ctx context.Context, workspace identity.WorkspaceID, ref string) (apitypes.App, error) {
	return c.setAppState(ctx, workspace, ref, AppDeleted)
}

// setAppState moves a live app to state under its row lock and wakes
// planning, which applies the change to containers and tasks.
func (c *Control) setAppState(ctx context.Context, workspace identity.WorkspaceID, ref string, state AppState) (apitypes.App, error) {
	var out apitypes.App
	err := pgx.BeginFunc(ctx, c.pool, func(tx pgx.Tx) error {
		q := c.queries.WithTx(tx)
		id, err := c.resolveApp(ctx, q, workspace, ref)
		if err != nil {
			return err
		}
		app, err := q.LockApp(ctx, LockAppParams{WorkspaceID: uuid.UUID(workspace), ID: id})
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return fmt.Errorf("lock app: %w", err)
		}
		if AppState(app.State) == AppDeleted {
			return ErrNotFound
		}
		if AppState(app.State) != state {
			if err := q.SetAppState(ctx, SetAppStateParams{ID: id, State: string(state)}); err != nil {
				return fmt.Errorf("set app state: %w", err)
			}
			if err := database.Notify(ctx, tx, database.ChannelExecution, id.String()); err != nil {
				return err
			}
		}
		out, err = c.appView(ctx, q, workspace, id)
		return err
	})
	if err != nil {
		return apitypes.App{}, fmt.Errorf("set app %s %s: %w", ref, state, err)
	}
	return out, nil
}

// resolveApp turns an app name or id into the id. A name resolves only a live
// app.
func (c *Control) resolveApp(ctx context.Context, q *Queries, workspace identity.WorkspaceID, ref string) (uuid.UUID, error) {
	if id, err := uuid.Parse(ref); err == nil {
		return id, nil
	}
	id, err := q.AppByName(ctx, AppByNameParams{WorkspaceID: uuid.UUID(workspace), Name: ref})
	if errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, ErrNotFound
	}
	if err != nil {
		return uuid.Nil, fmt.Errorf("find app %s: %w", ref, err)
	}
	return id, nil
}

func (c *Control) appView(ctx context.Context, q *Queries, workspace identity.WorkspaceID, id uuid.UUID) (apitypes.App, error) {
	row, err := q.AppView(ctx, AppViewParams{WorkspaceID: uuid.UUID(workspace), ID: id})
	if errors.Is(err, pgx.ErrNoRows) {
		return apitypes.App{}, ErrNotFound
	}
	if err != nil {
		return apitypes.App{}, fmt.Errorf("read app: %w", err)
	}
	return appOut(row), nil
}

func appOut(row AppViewRow) apitypes.App {
	return apitypes.App{
		Id: row.ID, Name: row.Name, State: apitypes.AppState(row.State), Workloads: int(row.Workloads),
		RunningContainers: int(row.RunningContainers), CreatedAt: row.CreatedAt,
	}
}

// maxPage bounds every page of a listing.
const maxPage = 1000

func pageSize(limit int) int32 {
	if limit <= 0 {
		return 100
	}
	return int32(min(limit, maxPage)) //nolint:gosec // Bounded by maxPage.
}
